import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import worker_callback
from app.models import Job, JobStatus
from conftest import TestingSessionLocal


CALLBACK_SECRET = "test-worker-callback-secret"
EXPECTED_FILE_IDS = {
    "result.json": "drive-result-json",
    "timeseries.csv": "drive-timeseries-csv",
    "manifest.json": "drive-manifest-json",
    "README.txt": "drive-readme",
    "metadata.json": "drive-metadata",
}


@pytest.fixture(autouse=True)
def configured_callback_secret(monkeypatch):
    monkeypatch.setattr(
        worker_callback,
        "settings",
        replace(worker_callback.settings, worker_callback_secret=CALLBACK_SECRET),
    )


def create_processing_job(client) -> str:
    registration = client.post(
        "/auth/register",
        json={"email": f"callback-{uuid4()}@example.com", "password": "correct horse battery staple"},
    )
    assert registration.status_code == 201
    job_id = str(uuid4())
    db = TestingSessionLocal()
    try:
        db.add(
            Job(
                job_id=job_id,
                user_id=registration.json()["id"],
                status=JobStatus.PROCESSING,
                started_at=datetime.now(timezone.utc),
                processing_metadata_json=json.dumps({"science_mode": "github_actions"}),
            )
        )
        db.commit()
    finally:
        db.close()
    return job_id


def completed_payload(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "status": "completed",
        "run_id": "36255377705",
        "result_folder_id": "drive-folder-id",
        "result_file_ids": EXPECTED_FILE_IDS,
        "science_commit": "77cc9646cfa46d3aff3669d351912f984cf67aa3",
    }


def callback(client, job_id: str, payload: dict, secret: str = CALLBACK_SECRET):
    return client.post(
        f"/worker/callback/{job_id}",
        json=payload,
        headers={"Authorization": f"Bearer {secret}"},
    )


def test_authenticated_completed_callback_persists_drive_references_and_metadata(client):
    job_id = create_processing_job(client)

    response = callback(client, job_id, completed_payload(job_id))

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "job_id": job_id,
        "job_status": "completed",
        "idempotent": False,
    }
    stored = client.get(f"/jobs/{job_id}").json()
    assert stored["status"] == "completed"
    assert stored["completed_at"] is not None
    assert stored["result_folder_id"] == "drive-folder-id"
    assert stored["result_file_ids"] == EXPECTED_FILE_IDS
    assert stored["error_code"] is None
    assert stored["processing_metadata"]["worker_callback"] == {
        "run_id": "36255377705",
        "status": "completed",
        "science_commit": "77cc9646cfa46d3aff3669d351912f984cf67aa3",
    }


def test_callback_rejects_invalid_secret(client):
    job_id = create_processing_job(client)

    response = callback(client, job_id, completed_payload(job_id), secret="invalid-secret")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "CALLBACK_TOKEN_INVALID"


def test_callback_rejects_missing_authentication(client):
    job_id = create_processing_job(client)

    response = client.post(f"/worker/callback/{job_id}", json=completed_payload(job_id))

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "CALLBACK_AUTH_MISSING"


def test_callback_rejects_malformed_payload_and_preserves_job(client):
    job_id = create_processing_job(client)

    response = client.post(
        f"/worker/callback/{job_id}",
        content="[]",
        headers={"Authorization": f"Bearer {CALLBACK_SECRET}", "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CALLBACK_INVALID_PAYLOAD"
    assert client.get(f"/jobs/{job_id}").json()["status"] == "processing"


def test_completed_callback_requires_exact_drive_result_metadata(client):
    job_id = create_processing_job(client)
    payload = completed_payload(job_id)
    payload["result_file_ids"] = {"result.json": "only-one-file"}

    response = callback(client, job_id, payload)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CALLBACK_RESULT_METADATA_INVALID"
    assert client.get(f"/jobs/{job_id}").json()["status"] == "processing"


def test_failed_callback_persists_safe_failure_state(client):
    job_id = create_processing_job(client)
    payload = {
        "job_id": job_id,
        "status": "failed",
        "run_id": "36255377705",
        "error_code": "GITHUB_ACTIONS_WORKER_FAILED",
        "error_message_safe": "GitHub Actions science worker failed",
    }

    response = callback(client, job_id, payload)

    assert response.status_code == 200
    assert response.json()["job_status"] == "failed"
    stored = client.get(f"/jobs/{job_id}").json()
    assert stored["status"] == "failed"
    assert stored["completed_at"] is not None
    assert stored["result_folder_id"] is None
    assert stored["result_file_ids"] is None
    assert stored["error_code"] == "GITHUB_ACTIONS_WORKER_FAILED"
    assert stored["error_message_safe"] == "GitHub Actions science worker failed"


def test_repeated_callback_is_idempotent_and_terminal_conflicts_are_rejected(client):
    job_id = create_processing_job(client)
    payload = completed_payload(job_id)

    first = callback(client, job_id, payload)
    completed_at = client.get(f"/jobs/{job_id}").json()["completed_at"]
    repeated = callback(client, job_id, payload)
    conflict = callback(
        client,
        job_id,
        {
            "job_id": job_id,
            "status": "failed",
            "run_id": "36255377705",
            "error_code": "GITHUB_ACTIONS_WORKER_FAILED",
            "error_message_safe": "GitHub Actions science worker failed",
        },
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert client.get(f"/jobs/{job_id}").json()["completed_at"] == completed_at
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "CALLBACK_TERMINAL_STATE_CONFLICT"
