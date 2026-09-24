from dataclasses import dataclass
from app.models import Job
from app.science.result_package import EXPECTED_RESULT_FILES, ResultPackage, validate_result_package
from app.storage.drive_service import DriveServiceError, DriveUploadResult


@dataclass
class _FakeFolder:
    folder_id: str
    files: dict[str, str]
    contents: dict[str, str]


class FakeDriveService:
    def __init__(self, fail_upload: bool = False):
        self.fail_upload = fail_upload
        self.folders: dict[str, _FakeFolder] = {}
        self.upload_calls = 0

    def upload_result_package(self, job_id: str, package: ResultPackage) -> DriveUploadResult:
        if self.fail_upload:
            raise DriveServiceError("DRIVE_UPLOAD_FAILED", "Drive upload failed safely")
        validate_result_package(package.directory)
        folder = self.folders.setdefault(
            job_id, _FakeFolder(f"fake-folder-{job_id}", {}, {})
        )
        if len(folder.files) == len(EXPECTED_RESULT_FILES):
            return DriveUploadResult(folder.folder_id, dict(folder.files))
        for name in EXPECTED_RESULT_FILES:
            if name not in folder.files:
                folder.files[name] = f"fake-file-{job_id}-{name}"
                folder.contents[name] = (package.directory / name).read_text(encoding="utf-8")
        self.upload_calls += 1
        return DriveUploadResult(folder.folder_id, dict(folder.files))

    def get_result(self, job: Job) -> dict[str, str]:
        folder = self.folders.get(job.job_id)
        if folder is None:
            raise DriveServiceError("DRIVE_RESULT_NOT_AVAILABLE", "Job result is not available")
        return {name: folder.contents[name] for name in EXPECTED_RESULT_FILES if name in folder.contents}
