"""Callback handler for SARGuardian GitHub Actions worker."""

import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Job, JobStatus
from app.science.result_package import EXPECTED_RESULT_FILES
from app.security import constant_time_compare


router = APIRouter(prefix="/worker", tags=["worker"])


class CallbackError(RuntimeError):
    def __init__(self, code: str, safe_message: str, status_code: int = 400):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,35}$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_:-]{0,99}$")
MAX_RUN_ID_LENGTH = 128
MAX_ERROR_MESSAGE_LENGTH = 1000
MAX_SCIENCE_COMMIT_LENGTH = 128


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


def _required_string(payload: dict[str, Any], key: str, maximum_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum_length:
        raise CallbackError(
            "CALLBACK_INVALID_PAYLOAD",
            f"{key} must be a non-empty string",
            status_code=400,
        )
    return value


def _optional_string(payload: dict[str, Any], key: str, maximum_length: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum_length:
        raise CallbackError(
            "CALLBACK_INVALID_PAYLOAD",
            f"{key} must be a non-empty string when provided",
            status_code=400,
        )
    return value


def _validate_callback_payload(job_id: str, payload: Any) -> dict[str, Any]:
    """Validate only the safe, worker-owned callback contract."""
    if not isinstance(payload, dict):
        raise CallbackError(
            "CALLBACK_INVALID_PAYLOAD",
            "Callback payload must be a JSON object",
            status_code=400,
        )
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise CallbackError(
            "CALLBACK_INVALID_JOB_ID",
            "Invalid job ID",
            status_code=400,
        )
    if payload.get("job_id") != job_id:
        raise CallbackError(
            "CALLBACK_JOB_ID_MISMATCH",
            "Job ID in payload does not match URL",
            status_code=400,
        )

    status_value = payload.get("status")
    if status_value not in ("completed", "failed"):
        raise CallbackError(
            "CALLBACK_INVALID_STATUS",
            "Status must be 'completed' or 'failed'",
            status_code=400,
        )

    validated: dict[str, Any] = {
        "job_id": job_id,
        "status": status_value,
        "run_id": _required_string(payload, "run_id", MAX_RUN_ID_LENGTH),
    }

    if status_value == "completed":
        result_folder_id = _required_string(payload, "result_folder_id", 255)
        result_file_ids = payload.get("result_file_ids")
        if (
            not isinstance(result_file_ids, dict)
            or not set(EXPECTED_RESULT_FILES).issubset(result_file_ids)
            or "metadata.json" not in result_file_ids
            or any(
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or ".." in name.split("/")
                or name.lower().endswith(".h5")
                for name in result_file_ids
            )
        ):
            raise CallbackError(
                "CALLBACK_RESULT_METADATA_INVALID",
                "Completed callbacks must include safe result file IDs and metadata",
                status_code=400,
            )
        if any(
            not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 255
            for file_id in result_file_ids.values()
        ):
            raise CallbackError(
                "CALLBACK_RESULT_METADATA_INVALID",
                "Completed callbacks must include non-empty result file IDs",
                status_code=400,
            )
        validated["result_folder_id"] = result_folder_id
        validated["result_file_ids"] = result_file_ids
        science_commit = _optional_string(payload, "science_commit", MAX_SCIENCE_COMMIT_LENGTH)
        if science_commit is not None:
            validated["science_commit"] = science_commit
    else:
        if payload.get("result_folder_id") is not None or payload.get("result_file_ids") is not None:
            raise CallbackError(
                "CALLBACK_RESULT_METADATA_INVALID",
                "Failed callbacks cannot include result metadata",
                status_code=400,
            )
        error_code = _optional_string(payload, "error_code", 100) or "GITHUB_ACTIONS_WORKER_FAILED"
        if not ERROR_CODE_PATTERN.fullmatch(error_code):
            raise CallbackError(
                "CALLBACK_INVALID_PAYLOAD",
                "error_code has an invalid format",
                status_code=400,
            )
        error_message_safe = (
            _optional_string(payload, "error_message_safe", MAX_ERROR_MESSAGE_LENGTH)
            or "GitHub Actions science worker failed"
        )
        validated["error_code"] = error_code
        validated["error_message_safe"] = error_message_safe

    return validated


def _record_callback_metadata(job: Job, payload: dict[str, Any]) -> None:
    try:
        metadata = json.loads(job.processing_metadata_json)
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    worker_metadata = {"run_id": payload["run_id"], "status": payload["status"]}
    if "science_commit" in payload:
        worker_metadata["science_commit"] = payload["science_commit"]
    metadata["worker_callback"] = worker_metadata
    job.processing_metadata_json = json.dumps(metadata)


@router.post("/callback/{job_id}")
async def handle_worker_callback(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _verify: None = Depends(verify_callback_auth),
) -> dict[str, Any]:
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

    payload = _validate_callback_payload(job_id, payload)

    job = db.scalar(select(Job).where(Job.job_id == job_id))
    if job is None:
        raise CallbackError(
            "CALLBACK_JOB_NOT_FOUND",
            "Job not found",
            status_code=404,
        )

    status_value = payload["status"]
    target_status = JobStatus.COMPLETED if status_value == "completed" else JobStatus.FAILED
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
        if job.status != target_status:
            raise CallbackError(
                "CALLBACK_TERMINAL_STATE_CONFLICT",
                "Job already has a different terminal status",
                status_code=409,
            )
        return {
            "status": "accepted",
            "job_id": job_id,
            "job_status": job.status.value,
            "idempotent": True,
        }

    if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
        raise CallbackError(
            "CALLBACK_INVALID_STATUS",
            "Job cannot accept a callback in its current state",
            status_code=409,
        )

    job.status = target_status
    job.completed_at = datetime.now(timezone.utc)
    _record_callback_metadata(job, payload)

    if status_value == "completed":
        job.result_folder_id = payload["result_folder_id"]
        job.result_file_ids_json = json.dumps(payload["result_file_ids"])
        job.error_code = None
        job.error_message_safe = None
    else:
        job.error_code = payload["error_code"]
        job.error_message_safe = payload["error_message_safe"]
        job.result_folder_id = None
        job.result_file_ids_json = None

    db.commit()
    return {
        "status": "accepted",
        "job_id": job_id,
        "job_status": job.status.value,
        "idempotent": False,
    }


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
