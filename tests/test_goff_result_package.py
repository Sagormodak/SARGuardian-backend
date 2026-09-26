import json
from pathlib import Path

import pytest

from scripts import upload_goff_result_to_drive as drive_upload


def test_drive_file_id_mapping_has_the_exact_result_package_names():
    uploaded_file_ids = {
        name: f"drive-id-for-{index}"
        for index, name in enumerate(drive_upload.EXPECTED_FILES, start=1)
    }

    mapping = json.loads(drive_upload.serialized_file_ids(uploaded_file_ids))

    assert mapping == uploaded_file_ids
    assert list(mapping) == list(drive_upload.EXPECTED_FILES)


def test_drive_file_id_mapping_rejects_missing_or_empty_ids():
    missing_id = {
        name: "drive-id"
        for name in drive_upload.EXPECTED_FILES
        if name != "README.txt"
    }
    empty_id = {name: "drive-id" for name in drive_upload.EXPECTED_FILES}
    empty_id["manifest.json"] = ""

    with pytest.raises(drive_upload.DriveUploadError):
        drive_upload.serialized_file_ids(missing_id)
    with pytest.raises(drive_upload.DriveUploadError):
        drive_upload.serialized_file_ids(empty_id)


def test_drive_validator_requires_the_worker_manifest_not_result_json(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "cleanup_status": "success",
            "final_processing_status": "completed",
            "processing_timings": [],
        }),
        encoding="utf-8",
    )

    drive_upload.verify_result_manifest(tmp_path)

    manifest_path.write_text(
        json.dumps({"raw_cleanup_success": True, "overall_success": True}),
        encoding="utf-8",
    )
    with pytest.raises(drive_upload.DriveUploadError):
        drive_upload.verify_result_manifest(tmp_path)


@pytest.mark.parametrize(
    "workflow_name",
    [
        "sarguardian-science-worker.yml",
        "nisar-goff-timeseries-test.yml",
    ],
)
def test_workflows_never_replace_the_manifest_with_result_json(workflow_name):
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / workflow_name
    ).read_text(encoding="utf-8")

    assert "cp result/result.json result/manifest.json" not in workflow


def test_worker_workflow_uses_the_verified_filename_id_mapping():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "sarguardian-science-worker.yml"
    ).read_text(encoding="utf-8")

    assert "DRIVE_FILE_IDS_JSON:" in workflow
    assert '"README.txt", "manifest.json", "result.json", "timeseries.csv"' in workflow
    assert "result_folder_id: $result_folder_id" in workflow
    assert "result_file_ids: $result_file_ids" in workflow


def test_worker_workflow_builds_parameters_after_step_env_is_available():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "sarguardian-science-worker.yml"
    ).read_text(encoding="utf-8")

    for name in ("JOB_ID", "TARGET_LAT", "TARGET_LON", "START_DATE", "BENCHMARK_ONLY"):
        assert f"{name}: ${{{{ inputs." in workflow
    assert 'PARAMETERS_JSON="$(' in workflow
    assert 'os.environ["TARGET_LAT"]' in workflow
    assert 'os.environ["TARGET_LON"]' in workflow
    assert 'os.environ["START_DATE"]' in workflow
    assert 'json.dumps(parameters, separators=(",", ":"))' in workflow
    assert '--parameters "$PARAMETERS_JSON"' in workflow
    assert 'worker_args+=(--benchmark-only)' in workflow


def test_worker_writes_a_detailed_manifest_in_benchmark_and_full_modes():
    worker = (Path(__file__).parents[1] / "scripts" / "sarguardian_worker.py").read_text(
        encoding="utf-8"
    )

    assert worker.count('(result_dir / "manifest.json").write_text') == 2
    assert worker.count('"cleanup_status": "success"') == 2
    assert worker.count('"final_processing_status": "completed"') == 2
