from dataclasses import dataclass
import json
from pathlib import Path
import shutil


EXPECTED_RESULT_FILES = (
    "result.json",
    "timeseries.csv",
    "manifest.json",
    "README.txt",
)


class ResultPackageError(RuntimeError):
    """A safe result-package validation failure."""


@dataclass(frozen=True)
class ResultPackage:
    directory: Path
    file_names: tuple[str, ...]
    raw_h5_count: int


def validate_result_package(directory: Path) -> ResultPackage:
    if not directory.is_dir():
        raise ResultPackageError("SCIENCE_RESULT_PACKAGE_MISSING")
    raw_h5_count = sum(
        path.is_file() and path.suffix.lower() == ".h5"
        for path in directory.rglob("*")
    )
    if raw_h5_count:
        raise ResultPackageError("SCIENCE_RESULT_PACKAGE_CONTAINS_RAW_H5")
    file_names = tuple(sorted(path.name for path in directory.iterdir() if path.is_file()))
    if set(file_names) != set(EXPECTED_RESULT_FILES):
        raise ResultPackageError("SCIENCE_RESULT_PACKAGE_INVALID_FILES")
    for file_name in EXPECTED_RESULT_FILES:
        if not (directory / file_name).is_file():
            raise ResultPackageError("SCIENCE_RESULT_PACKAGE_INVALID_FILES")
    try:
        json.loads((directory / "result.json").read_text(encoding="utf-8"))
        json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ResultPackageError("SCIENCE_RESULT_PACKAGE_INVALID_JSON") from None
    return ResultPackage(directory, file_names, raw_h5_count)


def prepare_result_directory(root: Path, job_id: str) -> Path:
    directory = root / job_id
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=False)
    return directory