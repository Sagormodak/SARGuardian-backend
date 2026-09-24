from pathlib import Path
from threading import Event
import time

from fastapi.testclient import TestClient

from app import job_worker
from app.main import app
from app.science.result_package import EXPECTED_RESULT_FILES
from app.science.service import MockScienceService, ScienceResult


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
    for _ in range(500):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def test_mock_job_completes_with_exact_result_package(tmp_path):
    client = TestClient(app)
    register(client, "science-success@example.com")
    job = create_job(client, {"target": "deterministic"})
    assert job["status"] == "queued"

    completed = wait_for_terminal(client, job["job_id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    assert completed["processing_metadata"]["science_mode"] == "mock"
    assert completed["processing_metadata"]["raw_h5_count"] == 0
    assert completed["result_folder_id"] is not None
    assert set(completed["result_file_ids"]) == {
        "result.json", "timeseries.csv", "manifest.json", "README.txt"
    }

    package_directory = tmp_path / "science-results" / job["job_id"]
    assert {path.name for path in package_directory.iterdir()} == set(EXPECTED_RESULT_FILES)
    assert not list(package_directory.rglob("*.h5"))


def test_mock_job_failure_is_safe():
    client = TestClient(app)
    register(client, "science-failure@example.com")
    job = create_job(client, {"mock_failure": True, "secret_like_value": "do-not-return"})

    failed = wait_for_terminal(client, job["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "MOCK_SCIENCE_FAILED"
    assert failed["error_message_safe"] == "Mock science failed safely"
    assert "do-not-return" not in failed["error_message_safe"]
    assert "Traceback" not in failed["error_message_safe"]


def test_ownership_is_enforced_while_other_job_is_processing(tmp_path):
    started = Event()
    release = Event()
    delegate = MockScienceService(tmp_path / "blocking-results")

    class BlockingScienceService:
        def run(self, job_id: str, parameters: dict) -> ScienceResult:
            started.set()
            assert release.wait(timeout=2)
            return delegate.run(job_id, parameters)

    job_worker.science_service_override = BlockingScienceService()
    user_a_client = TestClient(app)
    user_b_client = TestClient(app)
    register(user_a_client, "processing-a@example.com")
    register(user_b_client, "processing-b@example.com")
    user_a_job = create_job(user_a_client, {"owner": "a"})
    user_b_job = create_job(user_b_client, {"owner": "b"})

    assert started.wait(timeout=2)
    assert user_a_client.get(f"/jobs/{user_b_job['job_id']}").status_code == 404
    assert user_b_client.get(f"/jobs/{user_a_job['job_id']}").status_code == 404
    release.set()
    assert wait_for_terminal(user_a_client, user_a_job["job_id"])["status"] == "completed"
    assert wait_for_terminal(user_b_client, user_b_job["job_id"])["status"] == "completed"
