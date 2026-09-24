from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_register_login_me_and_logout_with_csrf():
    response = client.post(
        "/auth/register",
        json={"email": "User@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    assert response.json()["email"] == "user@example.com"
    assert client.get("/auth/me").status_code == 200

    csrf = client.cookies.get("sarguardian_csrf")
    logout = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert client.get("/auth/me").status_code == 401

    login = client.post(
        "/auth/login",
        json={"email": "USER@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_duplicate_email_and_invalid_credentials():
    payload = {"email": "user@example.com", "password": "correct horse battery staple"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409
    assert client.post(
        "/auth/login",
        json={"email": payload["email"], "password": "wrong password value"},
    ).status_code == 401


def test_logout_requires_csrf_token():
    response = client.post(
        "/auth/register",
        json={"email": "csrf@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    assert client.post("/auth/logout").status_code == 403
