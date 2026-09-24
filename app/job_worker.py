from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Job, JobStatus
from app.science.service import ScienceExecutionError, ScienceService, build_science_service
from app.storage.drive_service import DriveServiceError, DriveService, build_drive_service

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sarguardian-job")
science_service_override: ScienceService | None = None
drive_service_override: DriveService | None = None
session_factory_override = None


def submit_job(job_id: str) -> None:
    executor.submit(process_job, job_id)


def _new_session():
    return (session_factory_override or SessionLocal)()


def process_job(job_id: str) -> None:
    db = _new_session()
    job = None
    try:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        if job is None:
            return
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        db.commit()
        parameters = json.loads(job.processing_metadata_json)
        service = science_service_override or build_science_service()
        result = service.run(job.job_id, parameters)
        drive_service = drive_service_override or build_drive_service()
        drive_result = drive_service.upload_result_package(job.job_id, result.package)
        metadata = {**parameters, **result.metadata}
        job.processing_metadata_json = json.dumps(metadata)
        job.result_folder_id = drive_result.folder_id
        job.result_file_ids_json = json.dumps(drive_result.file_ids)
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.error_code = None
        job.error_message_safe = None
    except ScienceExecutionError as exc:
        job.status = JobStatus.FAILED
        job.error_code = exc.code
        job.error_message_safe = exc.safe_message
        job.completed_at = datetime.now(timezone.utc)
    except DriveServiceError as exc:
        job.status = JobStatus.FAILED
        job.error_code = exc.code
        job.error_message_safe = exc.safe_message
        job.completed_at = datetime.now(timezone.utc)
    except Exception:
        job.status = JobStatus.FAILED
        job.error_code = "SCIENCE_EXECUTION_FAILED"
        job.error_message_safe = "Science processing failed"
        job.completed_at = datetime.now(timezone.utc)
    finally:
        if job is not None:
            db.commit()
        db.close()