import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_csrf
from app.db import get_db
from app import job_worker
from app.models import Job, JobStatus, User
from app.schemas import JobCreate, JobResponse
from app.security import sanitize_metadata
from app.storage.drive_service import DriveServiceError

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_response(job: Job) -> JobResponse:
    metadata = json.loads(job.processing_metadata_json)
    result_file_ids = (
        json.loads(job.result_file_ids_json) if job.result_file_ids_json is not None else None
    )
    return JobResponse(
        job_id=job.job_id,
        user_id=job.user_id,
        status=job.status.value,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        processing_metadata=metadata,
        result_folder_id=job.result_folder_id,
        result_file_ids=result_file_ids,
        error_code=job.error_code,
        error_message_safe=job.error_message_safe,
    )


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate,
    _csrf: None = Depends(require_csrf),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobResponse:
    safe_metadata = sanitize_metadata(payload.processing_metadata)
    job = Job(
        user_id=user.id,
        status=JobStatus.QUEUED,
        processing_metadata_json=json.dumps(safe_metadata),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job_worker.submit_job(job.job_id)
    return _job_response(job)


@router.get("", response_model=list[JobResponse])
def list_jobs(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[JobResponse]:
    jobs = db.scalars(
        select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc())
    ).all()
    return [_job_response(job) for job in jobs]


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobResponse:
    job = db.scalar(select(Job).where(Job.job_id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _job_response(job)


@router.get("/{job_id}/result")
def get_job_result(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.scalar(select(Job).where(Job.job_id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    try:
        from app.job_worker import drive_service_override
        from app.storage.drive_service import build_drive_service

        service = drive_service_override or build_drive_service()
        return {"job_id": job.job_id, "files": service.get_result(job)}
    except DriveServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job result not found") from exc