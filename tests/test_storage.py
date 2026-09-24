from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app import job_worker
from app.main import app
from app.science.service import MockScienceService
from app.storage.drive_service import DriveServiceError
from app.storage.fake_drive_service import FakeDriveService


def register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response.json()


def create_job(client: TestClient, metadata: dict) -> dict:
    response = client.post(
        "/jobs",
        json={"processing_metadata": metadata},
        headers={"X-CSRF-Token": client.cookies.get("sarguardian_csrf")},
    )
    assert response.status_code == 201
    return response.json()


def wait_for_terminal(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def test_fake_upload_has_exact_contract_and_is_idempotent(tmp_path):
    package = MockScienceService(tmp_path / "science").run("job-1", {})
    drive = FakeDriveService()
    first = drive.upload_result_package("job-1", package.package)
    second = drive.upload_result_package("job-1", package.package)
    assert first == second
    assert set(first.file_ids) == {
        "result.json", "timeseries.csv", "manifest.json", "README.txt"
    }
    assert drive.upload_calls == 1
    assert not list((tmp_path / "science" / "job-1").rglob("*.h5"))


def test_fake_upload_rejects_h5(tmp_path):
    package_directory = tmp_path / "science" / "job-2"
    package = MockScienceService(tmp_path / "science").run("job-2", {}).package
    (package_directory / "raw.h5").write_bytes(b"raw")
    with pytest.raises(Exception):
        FakeDriveService().upload_result_package("job-2", package)


def test_job_stores_drive_references_after_verified_upload():
    client = TestClient(app)
    register(client, "storage-owner@example.com")
    created = create_job(client, {"purpose": "storage"})
    completed = wait_for_terminal(client, created["job_id"])
    assert completed["status"] == "completed"
    assert completed["result_folder_id"] is not None
    assert set(completed["result_file_ids"]) == {
        "result.json", "timeseries.csv", "manifest.json", "README.txt"
    }


def test_drive_failure_keeps_job_failed_and_safe():
    job_worker.drive_service_override = FakeDriveService(fail_upload=True)
    client = TestClient(app)
    register(client, "storage-failure@example.com")
    created = create_job(client, {})
    failed = wait_for_terminal(client, created["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "DRIVE_UPLOAD_FAILED"
    assert failed["error_message_safe"] == "Drive upload failed safely"
    assert failed["result_folder_id"] is None
    assert failed["result_file_ids"] is None


def test_completed_job_result_is_read_through_server_and_owned():
    user_a = TestClient(app)
    user_b = TestClient(app)
    register(user_a, "result-a@example.com")
    register(user_b, "result-b@example.com")
    job_a = wait_for_terminal(user_a, create_job(user_a, {})["job_id"])
    job_b = wait_for_terminal(user_b, create_job(user_b, {})["job_id"])

    result = user_a.get(f"/jobs/{job_a['job_id']}/result")
    assert result.status_code == 200
    assert set(result.json()["files"]) == {
        "result.json", "timeseries.csv", "manifest.json", "README.txt"
    }
    assert user_a.get(f"/jobs/{job_b['job_id']}/result").status_code == 404
    assert user_b.get(f"/jobs/{job_a['job_id']}/result").status_code == 404


def test_client_cannot_supply_drive_references_or_expose_credentials():
    client = TestClient(app)
    register(client, "storage-safety@example.com")
    created = create_job(
        client,
        {
            "result_folder_id": "attacker-folder",
            "result_file_ids": {"result.json": "attacker-file"},
            "GOOGLE_DRIVE_REFRESH_TOKEN": "do-not-return",
        },
    )
    completed = wait_for_terminal(client, created["job_id"])
    assert completed["result_folder_id"] != "attacker-folder"
    assert completed["result_file_ids"] != {"result.json": "attacker-file"}
    assert completed["processing_metadata"]["GOOGLE_DRIVE_REFRESH_TOKEN"] == "[REDACTED]"
    assert "do-not-return" not in str(completed)
