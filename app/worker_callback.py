"""Callback handler for SARGuardian GitHub Actions worker."""

import hmac
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Job, JobStatus
from app.security import constant_time_compare


router = APIRouter(prefix="/worker", tags=["worker"])


class CallbackError(RuntimeError):
    def __init__(self, code: str, safe_message: str, status_code: int = 400):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


def verify_callback_auth(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """Verify the callback authentication using HMAC or Bearer token."""
    secret = settings.worker_callback_secret
    if not secret:
        raise CallbackError(
            "CALLBACK_SECRET_NOT_CONFIGURED",
            "Worker callback secret not configured",
            status_code=500,
        )

    # Try HMAC signature first
    if x_signature:
        body = request.state.raw_body
        expected = hmac.new(
            secret.encode("utf-8"),
            body,
            "sha256",
        ).hexdigest()
        if constant_time_compare(expected, x_signature):
            return
        raise CallbackError(
            "CALLBACK_SIGNATURE_INVALID",
            "Invalid callback signature",
            status_code=401,
        )

    # Fall back to Bearer token
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
            if constant_time_compare(token, secret):
                return
        raise CallbackError(
            "CALLBACK_TOKEN_INVALID",
            "Invalid callback token",
            status_code=401,
        )

    raise CallbackError(
        "CALLBACK_AUTH_MISSING",
        "Missing X-Signature or Authorization header",
        status_code=401,
    )


@router.post("/callback/{job_id}")
async def handle_worker_callback(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _verify: None = Depends(verify_callback_auth),
) -> dict[str, str]:
    """
    Handle callback from GitHub Actions worker.

    Expected payload:
    {
        "job_id": "...",
        "status": "completed" | "failed",
        "run_id": "...",
        "result_folder_id": "...",  # optional
        "result_file_ids": {...},   # optional
        "error_code": "...",        # optional
        "error_message_safe": "..." # optional
    }
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise CallbackError(
            "CALLBACK_INVALID_JSON",
            "Invalid JSON payload",
            status_code=400,
        )

    if payload.get("job_id") != job_id:
        raise CallbackError(
            "CALLBACK_JOB_ID_MISMATCH",
            "Job ID in payload does not match URL",
            status_code=400,
        )

    job = db.scalar(select(Job).where(Job.job_id == job_id))
    if job is None:
        raise CallbackError(
            "CALLBACK_JOB_NOT_FOUND",
            "Job not found",
            status_code=404,
        )

    status_value = payload.get("status")
    if status_value not in ("completed", "failed"):
        raise CallbackError(
            "CALLBACK_INVALID_STATUS",
            "Status must be 'completed' or 'failed'",
            status_code=400,
        )

    job.status = JobStatus.COMPLETED if status_value == "completed" else JobStatus.FAILED
    job.completed_at = datetime.now(timezone.utc)

    if status_value == "completed":
        job.result_folder_id = payload.get("result_folder_id")
        if payload.get("result_file_ids"):
            job.result_file_ids_json = json.dumps(payload["result_file_ids"])
        job.error_code = None
        job.error_message_safe = None
    else:
        job.error_code = payload.get("error_code", "WORKER_FAILED")
        job.error_message_safe = payload.get("error_message_safe", "Worker reported failure")
        job.result_folder_id = None
        job.result_file_ids_json = None

    db.commit()
    return {"status": "accepted"}


def build_callback_payload(
    job_id: str,
    status: str,
    run_id: str,
    result_folder_id: str | None = None,
    result_file_ids: dict[str, str] | None = None,
    error_code: str | None = None,
    error_message_safe: str | None = None,
) -> dict[str, Any]:
    """Build a callback payload for the worker to send."""
    payload = {
        "job_id": job_id,
        "status": status,
        "run_id": run_id,
    }
    if result_folder_id:
        payload["result_folder_id"] = result_folder_id
    if result_file_ids:
        payload["result_file_ids"] = result_file_ids
    if error_code:
        payload["error_code"] = error_code
    if error_message_safe:
        payload["error_message_safe"] = error_message_safe
    return payload


def sign_callback_payload(payload: dict[str, Any], secret: str) -> str:
    """Sign a callback payload with HMAC-SHA256."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        body,
        "sha256",
    ).hexdigest()