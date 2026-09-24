from fastapi.testclient import TestClient
import time
from app.main import app



def register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response.json()


def create_job(client: TestClient, metadata: dict) -> dict:
    csrf = client.cookies.get("sarguardian_csrf")
    response = client.post(
        "/jobs",
        json={"processing_metadata": metadata},
        headers={"X-CSRF-Token": csrf},
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


def test_two_users_are_isolated_and_client_user_id_cannot_override_identity():
    user_a_client = TestClient(app)
    user_b_client = TestClient(app)
    user_a = register(user_a_client, "user-a@example.com")
    user_b = register(user_b_client, "user-b@example.com")

    user_a_job = create_job(user_a_client, {"source": "a"})
    user_b_job_response = user_b_client.post(
        "/jobs",
        json={"user_id": user_a["id"], "processing_metadata": {"source": "b"}},
        headers={"X-CSRF-Token": user_b_client.cookies.get("sarguardian_csrf")},
    )
    assert user_b_job_response.status_code == 201
    user_b_job = user_b_job_response.json()

    assert user_a_job["user_id"] == user_a["id"]
    assert user_b_job["user_id"] == user_b["id"]
    assert user_a_job["job_id"] != user_b_job["job_id"]
    user_a_jobs = user_a_client.get("/jobs").json()
    user_b_jobs = user_b_client.get("/jobs").json()
    assert len(user_a_jobs) == 1
    assert len(user_b_jobs) == 1
    assert user_a_jobs[0]["job_id"] == user_a_job["job_id"]
    assert user_b_jobs[0]["job_id"] == user_b_job["job_id"]
    assert user_a_jobs[0]["user_id"] == user_a["id"]
    assert user_b_jobs[0]["user_id"] == user_b["id"]
    assert user_a_client.get(f"/jobs/{user_b_job['job_id']}").status_code == 404
    assert user_b_client.get(f"/jobs/{user_a_job['job_id']}").status_code == 404
    wait_for_terminal(user_a_client, user_a_job["job_id"])
    wait_for_terminal(user_b_client, user_b_job["job_id"])


def test_job_endpoints_require_authentication():
    anonymous = TestClient(app)
    assert anonymous.get("/jobs").status_code == 401
    assert anonymous.get("/jobs/unknown").status_code == 401
    assert anonymous.post("/jobs", json={}).status_code == 403


def test_job_defaults_are_queued_and_drive_references_are_nullable():
    client = TestClient(app)
    register(client, "queued@example.com")
    job = create_job(client, {"product": "goff"})
    assert job["status"] == "queued"
    job = wait_for_terminal(client, job["job_id"])
    assert job["status"] == "completed"
    assert job["processing_metadata"]["product"] == "goff"
    assert job["processing_metadata"]["science_mode"] == "mock"
    assert job["result_folder_id"] is not None
    assert set(job["result_file_ids"]) == {
        "result.json", "timeseries.csv", "manifest.json", "README.txt"
    }
    assert job["completed_at"] is not None
    assert job["error_code"] is None
    assert job["error_message_safe"] is None
