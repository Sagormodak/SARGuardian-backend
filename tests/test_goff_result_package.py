import json
from pathlib import Path

import pytest

from scripts import upload_goff_result_to_drive as drive_upload
from scripts import sarguardian_worker


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


@pytest.mark.parametrize("http_status", (400, 403, 404, 429, 500))
def test_drive_folder_creation_reports_safe_parent_lookup_http_status(
    monkeypatch, http_status
):
    def fail_parent_lookup(*_args, **_kwargs):
        raise drive_upload.DriveRequestError(http_status=http_status)

    monkeypatch.setattr(drive_upload, "drive_request", fail_parent_lookup)

    with pytest.raises(drive_upload.DriveFolderCreationError) as exc_info:
        drive_upload.create_run_folder("access-token", "parent-id", "run-id")

    assert str(exc_info.value) == "DRIVE_FOLDER_CREATION_FAILED"
    assert exc_info.value.diagnostic == (
        f"DRIVE_PARENT_LOOKUP_FAILED: http_status={http_status}"
    )


def test_drive_folder_creation_reports_safe_parent_folder_validation(monkeypatch, capsys):
    monkeypatch.setattr(
        drive_upload,
        "drive_request",
        lambda *_args, **_kwargs: {"mimeType": "text/plain"},
    )

    with pytest.raises(drive_upload.DriveFolderCreationError) as exc_info:
        drive_upload.create_run_folder("access-token", "parent-id", "run-id")

    assert exc_info.value.diagnostic == "DRIVE_PARENT_FOLDER_INVALID"
    assert "DRIVE_PARENT_LOOKUP_SUCCEEDED" in capsys.readouterr().out


def test_drive_folder_creation_reports_safe_post_network_failure(monkeypatch, capsys):
    calls = []

    def drive_request(*_args, method="GET", **_kwargs):
        calls.append(method)
        if method == "GET":
            return {"mimeType": "application/vnd.google-apps.folder"}
        raise drive_upload.DriveRequestError()

    monkeypatch.setattr(drive_upload, "drive_request", drive_request)

    with pytest.raises(drive_upload.DriveFolderCreationError) as exc_info:
        drive_upload.create_run_folder("access-token", "parent-id", "run-id")

    assert exc_info.value.diagnostic == "DRIVE_FOLDER_CREATE_FAILED: network_or_timeout"
    assert calls == ["GET", "POST"]
    assert "DRIVE_PARENT_FOLDER_CONFIRMED" in capsys.readouterr().out


def test_drive_folder_creation_reports_safe_post_http_status(monkeypatch, capsys):
    def drive_request(*_args, method="GET", **_kwargs):
        if method == "GET":
            return {"mimeType": "application/vnd.google-apps.folder"}
        raise drive_upload.DriveRequestError(http_status=403)

    monkeypatch.setattr(drive_upload, "drive_request", drive_request)

    with pytest.raises(drive_upload.DriveFolderCreationError) as exc_info:
        drive_upload.create_run_folder("access-token", "parent-id", "run-id")

    assert exc_info.value.diagnostic == "DRIVE_FOLDER_CREATE_FAILED: http_status=403"
    output = capsys.readouterr().out
    assert "DRIVE_PARENT_LOOKUP_SUCCEEDED" in output
    assert "DRIVE_PARENT_FOLDER_CONFIRMED" in output
    assert "DRIVE_FOLDER_CREATE_REQUEST_STARTED" in output


def test_main_preserves_safe_folder_creation_diagnostic(monkeypatch, capsys):
    def fail_folder_creation(*_args):
        raise drive_upload.DriveFolderCreationError(
            "DRIVE_FOLDER_CREATE_FAILED: http_status=403"
        )

    monkeypatch.setattr(drive_upload, "required_environment", lambda _name: "value")
    monkeypatch.setattr(
        drive_upload, "parse_client_configuration", lambda _value: ("client-id", "secret")
    )
    monkeypatch.setattr(drive_upload, "parse_refresh_token", lambda _value: "refresh")
    monkeypatch.setattr(drive_upload, "result_files", lambda _path: [])
    monkeypatch.setattr(drive_upload, "verify_result_manifest", lambda _path: None)
    monkeypatch.setattr(drive_upload, "refresh_access_token", lambda *_args: "access")
    monkeypatch.setattr(drive_upload, "verify_authenticated_account", lambda _token: None)
    monkeypatch.setattr(
        drive_upload,
        "create_run_folder",
        fail_folder_creation,
    )

    assert drive_upload.main() == 1

    output = capsys.readouterr().out
    assert "DRIVE_FOLDER_CREATE_FAILED: http_status=403" in output
    assert "DRIVE_FOLDER_CREATION_FAILED" in output
    assert "DRIVE_UPLOAD_FAILED" in output


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


def test_worker_workflow_checks_the_dispatch_revision_and_result_files():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "sarguardian-science-worker.yml"
    ).read_text(encoding="utf-8")

    assert "ref: ${{ github.sha }}" in workflow
    assert "run: git rev-parse HEAD" in workflow
    for command in (
        "pwd",
        "ls -lah .",
        "ls -lah result/",
        "stat result/manifest.json",
        "file result/manifest.json",
    ):
        assert command in workflow
    assert 'cd "$GITHUB_WORKSPACE"' in workflow

    worker_step = workflow.split("      - name: Complete worker-generated result package")[0]
    package_step = workflow.split("      - name: Complete worker-generated result package", 1)[1].split(
        "      - name: Upload GOFF result package to Google Drive", 1
    )[0]

    for command in (
        "test -f result/result.json",
        "test -f result/timeseries.csv",
        "test -f result/manifest.json",
    ):
        assert command in worker_step
        assert command in package_step
    assert "test -f result/README.txt" not in worker_step
    assert "test -f result/README.txt" in package_step


def test_worker_writes_a_detailed_manifest_in_benchmark_and_full_modes():
    worker = (Path(__file__).parents[1] / "scripts" / "sarguardian_worker.py").read_text(
        encoding="utf-8"
    )

    assert worker.count("write_result_package_atomically(") == 2
    assert worker.count('(result_dir / "manifest.json").write_text') == 1
    assert worker.count('"cleanup_status": "success"') == 2
    assert worker.count('"final_processing_status": "completed"') == 2


def test_atomic_result_package_publish_requires_exact_four_files(tmp_path):
    result_directory = tmp_path / "result"
    package = {
        name: f"contents for {name}\n"
        for name in sarguardian_worker.EXPECTED_RESULT_FILES
    }

    sarguardian_worker.write_result_package_atomically(result_directory, package)

    assert {path.name for path in result_directory.iterdir()} == set(package)
    assert all((result_directory / name).read_text(encoding="utf-8") == content
               for name, content in package.items())
    with pytest.raises(RuntimeError, match="RESULT_PACKAGE_OUTPUT_EXISTS"):
        sarguardian_worker.write_result_package_atomically(result_directory, package)
