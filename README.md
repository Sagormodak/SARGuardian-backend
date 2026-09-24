# SARGuardian Backend

A FastAPI backend foundation for multi-user SARGuardian jobs. User authentication is separate from the central server-side Google Drive storage identity.

## Local setup

```powershell
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python scripts/init_db.py
python -m uvicorn app.main:app --reload
```

The API runs at `http://127.0.0.1:8000`. Health check: `GET /health`.

## Authentication endpoints

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout` with `X-CSRF-Token`
- `GET /auth/me`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`

Passwords are hashed with Argon2id. Sessions use opaque tokens whose hashes are stored in the database. The session cookie is HttpOnly; the CSRF cookie is readable by the application so it can be copied into the `X-CSRF-Token` header.

## Database

SQLite is the local default. Set `DATABASE_URL` to a PostgreSQL SQLAlchemy URL for deployment. Run `python scripts/init_db.py` locally; Alembic configuration is present under `alembic/` for migration revisions.

Google Drive settings are server-side only and are intentionally empty in `.env.example`. They must never be returned by API responses or sent to the browser.

## Jobs

Authenticated `POST /jobs` requests create queued, user-owned job records only. Science execution and Drive result storage are added in later phases. The server derives ownership from the authenticated session; a client-provided `user_id` is ignored.

## Science execution

`SCIENCE_MODE=mock` is the local default. It performs no Earthdata/NASA access and writes a deterministic four-file result package under `SCIENCE_RESULT_ROOT/<job_id>`.

`SCIENCE_MODE=real` invokes the configured `SCIENCE_REAL_COMMAND` after verifying that `SCIENCE_ROOT` is at commit `SCIENCE_EXPECTED_COMMIT`. The external runner must preserve the existing pinned GOFF processing and write the same four-file package. This backend layer does not copy or rewrite the scientific algorithm.

## Central Drive storage

Drive storage is server-side only. Set `DRIVE_MODE=real` and provide these values through the backend environment or secret manager:

- `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`
- `GOOGLE_DRIVE_REFRESH_TOKEN`
- `GOOGLE_DRIVE_PARENT_FOLDER_ID`

The refresh token must belong to the central `sarguardian.org@gmail.com` storage identity, not an end-user account. Never place these values in frontend configuration, browser storage, API responses, source files, or logs. `DRIVE_MODE=mock` is used for local tests and does not contact Google Drive.

## Tests

```powershell
pytest
```

The science source and GitHub Actions workflows remain separate from this application foundation.
