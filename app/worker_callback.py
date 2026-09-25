from datetime import datetime, timezone
import hmac
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Job, JobStatus


router = APIRouter(
    prefix="/internal/worker",
    tags=["internal-worker"],
)


class WorkerCallback(BaseModel):
    status: Literal["processing", "completed", "failed"]
    result_folder_id: str | None = Field(default=None, max_length=255)
    result_file_ids: dict[str, str] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message_safe: str | None = None


def _verify_callback_secret(authorization: str | None) -> None:
    expected = settings.worker_callback_secret.strip()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker callback is not configured",
        )

    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    supplied = authorization[len(prefix):].strip()

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


@router.post("/jobs/{job_id}/callback")
def worker_callback(
    job_id: str,
    payload: WorkerCallback,
    authorization: str | None = Header(default=None),
    db: Session = next(get_db()),
) -> dict[str, str]:
    _verify_callback_secret(authorization)

    job = db.scalar(
        select(Job).where(Job.job_id == job_id)
    )

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    now = datetime.now(timezone.utc)

    if payload.status == "processing":
        if job.status in (
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
        ):
            job.status = JobStatus.PROCESSING
            if job.started_at is None:
                job.started_at = now

    elif payload.status == "completed":
        job.status = JobStatus.COMPLETED
        job.completed_at = now
        job.error_code = None
        job.error_message_safe = None

        if payload.result_folder_id is not None:
            job.result_folder_id = payload.result_folder_id

        if payload.result_file_ids is not None:
            import json

            job.result_file_ids_json = json.dumps(
                payload.result_file_ids
            )

    elif payload.status == "failed":
        job.status = JobStatus.FAILED
        job.completed_at = now

        job.error_code = (
            payload.error_code
            or "SCIENCE_EXECUTION_FAILED"
        )

        job.error_message_safe = (
            payload.error_message_safe
            or "Science processing failed"
        )

    db.commit()

    return {
        "status": "accepted",
        "job_id": job.job_id,
    }
