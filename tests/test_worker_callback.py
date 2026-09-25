from datetime import datetime

from app.config import settings
from app.models import Job, JobStatus, User
from app.security import hash_password


def set_callback_secret(value: str) -> None:
    object.__setattr__(
        settings,
        "worker_callback_secret",
        value,
    )


def create_user_and_job(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "worker@example.com",
            "password": "worker-password-123",
        },
    )
    assert response.status_code == 201

    login = client.post(
        "/auth/login",
        json={
            "email": "worker@example.com",
            "password": "worker-password-123",
        },
    )
    assert login.status_code == 200

    return client


def test_worker_callback_rejects_missing_secret(client):
    set_callback_secret("test-callback-secret")

    response = client.post(
        "/internal/worker/jobs/not-a-real-job/callback",
        json={"status": "processing"},
    )

    assert response.status_code == 401


def test_worker_callback_completes_job(client):
    from tests.conftest import TestingSessionLocal

    client = create_user_and_job(client)

    create_response = client.post(
        "/jobs",
        json={"processing_metadata": {"mode": "mock"}},
        headers={"X-CSRF-Token": client.cookies.get("sarguardian_csrf")},
    )
    assert create_response.status_code == 201

    job_id = create_response.json()["job_id"]

    db = TestingSessionLocal()
    job = db.get(Job, job_id)
    assert job is not None
    job.status = JobStatus.PROCESSING
    db.commit()
    db.close()

    set_callback_secret("test-callback-secret")

    response = client.post(
        f"/internal/worker/jobs/{job_id}/callback",
        json={
            "status": "completed",
            "result_folder_id": "folder-123",
            "result_file_ids": {
                "result.json": "file-1",
                "timeseries.csv": "file-2",
                "manifest.json": "file-3",
                "README.txt": "file-4",
            },
        },
        headers={
            "Authorization": "Bearer test-callback-secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    db = TestingSessionLocal()
    job = db.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.result_folder_id == "folder-123"
    assert job.result_file_ids_json is not None
    assert job.completed_at is not None
    db.close()
