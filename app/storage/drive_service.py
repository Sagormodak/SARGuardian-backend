from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.models import Job
from app.science.result_package import EXPECTED_RESULT_FILES, ResultPackage, validate_result_package
from app.storage.drive_client import DRIVE_FOLDER_MIME, DriveClient, DriveClientError


MIME_TYPES = {
    "result.json": "application/json",
    "timeseries.csv": "text/csv",
    "manifest.json": "application/json",
    "README.txt": "text/plain",
}
EXPECTED_ACCOUNT = "sarguardian.org@gmail.com"


class DriveServiceError(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class DriveUploadResult:
    folder_id: str
    file_ids: dict[str, str]


class DriveService(Protocol):
    def upload_result_package(self, job_id: str, package: ResultPackage) -> DriveUploadResult:
        """Upload and verify one job package."""

    def get_result(self, job: Job) -> dict[str, str]:
        """Read result files using only server-side job references."""


class RealDriveService:
    def __init__(self, client: DriveClient | None = None, parent_folder_id: str | None = None):
        self.client = client or DriveClient.from_environment()
        self.parent_folder_id = parent_folder_id or __import__("os").environ.get(
            "GOOGLE_DRIVE_PARENT_FOLDER_ID", ""
        ).strip()
        if not self.parent_folder_id:
            raise DriveServiceError("DRIVE_CONFIGURATION_INVALID", "Drive storage is not configured")

    def upload_result_package(self, job_id: str, package: ResultPackage) -> DriveUploadResult:
        try:
            validate_result_package(package.directory)
            parent = self.client.get_file(self.parent_folder_id)
            if parent.get("mimeType") != DRIVE_FOLDER_MIME:
                raise DriveServiceError("DRIVE_PARENT_FOLDER_INVALID", "Drive parent folder is invalid")
            folder_id = self._find_or_create_job_folder(job_id)
            files = self.client.list_children(folder_id)
            existing = self._validate_existing_files(files)
            file_ids = dict(existing)
            for file_name in EXPECTED_RESULT_FILES:
                if file_name not in file_ids:
                    file_ids[file_name] = self.client.upload_file(
                        folder_id, package.directory / file_name, MIME_TYPES[file_name]
                    )
            verified = self._validate_existing_files(self.client.list_children(folder_id))
            if set(verified) != set(EXPECTED_RESULT_FILES):
                raise DriveServiceError("DRIVE_VERIFICATION_FAILED", "Drive result verification failed")
            return DriveUploadResult(folder_id, verified)
        except DriveServiceError:
            raise
        except DriveClientError as exc:
            raise DriveServiceError(exc.code, "Drive storage operation failed") from None
        except OSError:
            raise DriveServiceError("DRIVE_RESULT_PACKAGE_INVALID", "Result package is unavailable") from None

    def get_result(self, job: Job) -> dict[str, str]:
        if not job.result_file_ids_json:
            raise DriveServiceError("DRIVE_RESULT_NOT_AVAILABLE", "Job result is not available")
        try:
            file_ids = json.loads(job.result_file_ids_json)
        except json.JSONDecodeError:
            raise DriveServiceError("DRIVE_RESULT_REFERENCES_INVALID", "Job result references are invalid") from None
        if not isinstance(file_ids, dict) or set(file_ids) != set(EXPECTED_RESULT_FILES):
            raise DriveServiceError("DRIVE_RESULT_REFERENCES_INVALID", "Job result references are invalid")
        try:
            return {
                name: self.client.download_file(file_ids[name]).decode("utf-8")
                for name in EXPECTED_RESULT_FILES
            }
        except (DriveClientError, UnicodeDecodeError):
            raise DriveServiceError("DRIVE_RESULT_READ_FAILED", "Drive result could not be read") from None

    def health_check(self) -> dict[str, int | bool | str]:
        try:
            token_status = self.client.refresh_access_token()
            _, email = self.client.about_email()
            parent = self.client.get_file(self.parent_folder_id)
            return {
                "token_refresh_status": token_status,
                "account_match": isinstance(email, str) and email.lower() == EXPECTED_ACCOUNT,
                "parent_status": 200,
                "parent_is_drive_folder": parent.get("mimeType") == DRIVE_FOLDER_MIME,
            }
        except DriveClientError as exc:
            return {
                "token_refresh_status": exc.status or "ERROR",
                "account_match": False,
                "parent_status": exc.status or "NOT_RUN",
                "parent_is_drive_folder": False,
            }

    def _find_or_create_job_folder(self, job_id: str) -> str:
        matches = [file for file in self.client.list_children(self.parent_folder_id) if file.get("name") == job_id]
        folders = [file for file in matches if file.get("mimeType") == DRIVE_FOLDER_MIME]
        if any(file.get("mimeType") != DRIVE_FOLDER_MIME for file in matches):
            raise DriveServiceError("DRIVE_JOB_FOLDER_INVALID", "Drive job entry is not a folder")
        if len(folders) > 1:
            raise DriveServiceError("DRIVE_DUPLICATE_JOB_FOLDERS", "Duplicate Drive job folders found")
        if folders:
            return folders[0]["id"]
        return self.client.create_folder(self.parent_folder_id, job_id)

    @staticmethod
    def _validate_existing_files(files: list[dict]) -> dict[str, str]:
        result: dict[str, str] = {}
        for file in files:
            name = file.get("name")
            if not isinstance(name, str):
                continue
            if name.lower().endswith(".h5") or name not in EXPECTED_RESULT_FILES:
                raise DriveServiceError("DRIVE_UNEXPECTED_FILES", "Drive result folder contains unexpected files")
            file_id = file.get("id")
            if isinstance(file_id, str):
                if name in result:
                    raise DriveServiceError("DRIVE_DUPLICATE_FILES", "Drive result folder contains duplicate files")
                result[name] = file_id
        return result


_default_fake_service = None


def build_drive_service() -> DriveService:
    global _default_fake_service
    if settings.drive_mode == "mock":
        if _default_fake_service is None:
            from app.storage.fake_drive_service import FakeDriveService

            _default_fake_service = FakeDriveService()
        return _default_fake_service
    if settings.drive_mode == "real":
        return RealDriveService()
    raise DriveServiceError("DRIVE_MODE_INVALID", "Drive mode is invalid")
