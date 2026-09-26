from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Protocol

from app.config import settings
from app.science.result_package import (
    EXPECTED_RESULT_FILES,
    ResultPackage,
    prepare_result_directory,
    validate_result_package,
)
from app.worker_dispatch import DispatchError, dispatch_job


class ScienceExecutionError(RuntimeError):
    """A safe, user-facing science execution failure."""

    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class ScienceResult:
    package: ResultPackage | None
    metadata: dict[str, Any]


class ScienceService(Protocol):
    def run(self, job_id: str, parameters: dict[str, Any]) -> ScienceResult:
        """Run science for one job and return a validated result package."""


class MockScienceService:
    def __init__(self, output_root: Path | None = None):
        self.output_root = output_root or Path(settings.science_result_root)

    def run(self, job_id: str, parameters: dict[str, Any]) -> ScienceResult:
        if parameters.get("mock_failure") is True:
            raise ScienceExecutionError("MOCK_SCIENCE_FAILED", "Mock science failed safely")
        directory = prepare_result_directory(self.output_root, job_id)
        result = {
            "job_id": job_id,
            "mode": "mock",
            "processing_metadata": parameters,
        }
        manifest = {
            "job_id": job_id,
            "science_mode": "mock",
            "raw_cleanup_success": True,
            "result_files": list(EXPECTED_RESULT_FILES),
        }
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        (directory / "timeseries.csv").write_text(
            "geometry,component,epoch,cumulative_mm\nMOCK,1,2025-01-01,0.0\n",
            encoding="utf-8",
        )
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "README.txt").write_text(
            "SARGuardian deterministic mock science result.\n", encoding="utf-8"
        )
        package = validate_result_package(directory)
        return ScienceResult(
            package=package,
            metadata={
                "science_mode": "mock",
                "result_files": list(package.file_names),
                "raw_h5_count": package.raw_h5_count,
            },
        )


class RealScienceService:
    """Adapter for the existing pinned science runner; it does not reimplement science."""

    def run(self, job_id: str, parameters: dict[str, Any]) -> ScienceResult:
        if not settings.science_real_command:
            raise ScienceExecutionError(
                "REAL_SCIENCE_NOT_CONFIGURED", "Real science is not configured"
            )
        science_root = Path(settings.science_root)
        output_directory = prepare_result_directory(Path(settings.science_result_root), job_id)
        try:
            actual_commit = subprocess.check_output(
                ["git", "-C", str(science_root), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=30,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            raise ScienceExecutionError(
                "SCIENCE_SOURCE_INVALID", "Pinned science source is unavailable"
            ) from None
        if actual_commit != settings.science_expected_commit:
            raise ScienceExecutionError(
                "SCIENCE_SOURCE_REVISION_MISMATCH", "Pinned science source revision mismatch"
            )
        command = shlex.split(settings.science_real_command)
        environment = {
            "SCIENCE_ROOT": str(science_root),
            "SCIENCE_OUTPUT_DIR": str(output_directory),
            "SCIENCE_JOB_ID": job_id,
            "SCIENCE_PARAMETERS_JSON": json.dumps(parameters),
        }
        try:
            subprocess.run(
                command,
                check=True,
                cwd=Path.cwd(),
                env={**_safe_process_environment(), **environment},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60 * 60,
            )
        except (OSError, subprocess.SubprocessError):
            raise ScienceExecutionError(
                "SCIENCE_EXECUTION_FAILED", "Science processing failed"
            ) from None
        package = validate_result_package(output_directory)
        return ScienceResult(
            package=package,
            metadata={
                "science_mode": "real",
                "science_commit": actual_commit,
                "result_files": list(package.file_names),
                "raw_h5_count": package.raw_h5_count,
            },
        )


class GitHubActionsScienceService:
    """Dispatches science execution to GitHub Actions workflow."""

    def run(self, job_id: str, parameters: dict[str, Any]) -> ScienceResult:
        benchmark_only = parameters.get("benchmark_only", False)
        try:
            run_id = dispatch_job(job_id, parameters, benchmark_only)
        except DispatchError as exc:
            raise ScienceExecutionError(exc.code, exc.safe_message) from exc
        return ScienceResult(
            package=None,
            metadata={
                "science_mode": "github_actions",
                "dispatched": True,
                "run_id": run_id,
                "benchmark_only": benchmark_only,
            },
        )


def _safe_process_environment() -> dict[str, str]:
    import os

    return dict(os.environ)


def build_science_service() -> ScienceService:
    if settings.science_mode == "mock":
        return MockScienceService()
    if settings.science_mode == "real":
        return RealScienceService()
    if settings.science_mode == "github_actions":
        return GitHubActionsScienceService()
    raise ScienceExecutionError("SCIENCE_MODE_INVALID", "Science mode is invalid")