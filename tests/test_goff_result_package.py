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
    monkeypatch.setattr(drive_upload, "write_metadata", lambda _path, _job_id: {"output_inventory": []})
    monkeypatch.setattr(drive_upload, "refresh_access_token", lambda *_args: "access")
    monkeypatch.setattr(drive_upload, "verify_authenticated_account", lambda _token: None)
    monkeypatch.setattr(
        drive_upload,
        "find_or_create_child_folder",
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
    assert '"README.txt", "manifest.json", "metadata.json", "result.json", "timeseries.csv"' in workflow
    assert "result_folder_id: $result_folder_id" in workflow
    assert "result_file_ids: $result_file_ids" in workflow


def test_worker_workflow_requires_an_accepted_authenticated_callback_when_configured():
    workflow = (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "sarguardian-science-worker.yml"
    ).read_text(encoding="utf-8")

    assert "RENDER_CALLBACK_URL: ${{ vars.RENDER_CALLBACK_URL }}" in workflow
    assert "WORKER_CALLBACK_SECRET: ${{ secrets.WORKER_CALLBACK_SECRET }}" in workflow
    assert 'CALLBACK_ENDPOINT="${RENDER_CALLBACK_URL%/}/worker/callback/${{ inputs.job_id }}"' in workflow
    assert '"Authorization: Bearer ${WORKER_CALLBACK_SECRET}"' in workflow
    assert "GITHUB_ACTIONS_WORKER_FAILED" in workflow
    assert "WORKER_CALLBACK_ACCEPTED: status=$STATUS" in workflow
    assert "--fail-with-body" in workflow
    assert ".status == \"accepted\"" in workflow
    assert '|| echo "Callback failed but continuing"' not in workflow


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
        'WORK_OUTPUT_DIR="$(mktemp -d "${RUNNER_TEMP}/sarguardian-${JOB_ID}-XXXXXX")"',
        'ls -lah "$WORK_OUTPUT_DIR/result/"',
        'stat "$result_dir/manifest.json"',
        'file "$result_dir/manifest.json"',
    ):
        assert command in workflow
    assert 'cd "$GITHUB_WORKSPACE"' not in workflow

    worker_step = workflow.split("      - name: Complete worker-generated result package")[0]
    package_step = workflow.split("      - name: Complete worker-generated result package", 1)[1].split(
        "      - name: Upload GOFF result package to Google Drive", 1
    )[0]

    for command in (
        "result.json",
        "timeseries.csv",
        "manifest.json",
    ):
        assert command in worker_step
        assert command in package_step
    assert "README.txt" not in worker_step
    assert "README.txt" in package_step
    assert "actions/upload-artifact" not in workflow
    assert "Remove temporary user science outputs" in workflow
    assert 'rm -rf -- "$SARGUARDIAN_WORK_OUTPUT_DIR"' in workflow


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


class _RuntimeAoiReader:
    def __init__(self):
        self.AOIS = {"source": [(85.4, 28.2), (85.6, 28.2), (85.5, 28.4)]}
        self.selected = None

    def set_aoi(self, name):
        self.selected = name


def test_runtime_aoi_is_validated_and_controls_the_reader_grid():
    reader = _RuntimeAoiReader()
    aoi = {
        "type": "Polygon",
        "coordinates": [[[85.0, 28.0], [85.2, 28.0], [85.1, 28.2], [85.0, 28.0]]],
    }

    context = sarguardian_worker.runtime_aoi_context({"aoi": aoi}, reader)

    assert reader.selected == "runtime"
    assert context["aoi_mode"] == "user"
    assert context["bbox"] == (85.0, 28.0, 85.2, 28.2)
    assert context["centroid"]["lon"] == pytest.approx(85.1)
    assert context["centroid"]["lat"] == pytest.approx(28.066666666666666)
    assert len(context["aoi_hash"]) == 64


def test_user_aoi_defaults_legacy_target_coordinates_to_its_centroid():
    context = {
        "aoi_mode": "user",
        "centroid": {"lon": 85.1, "lat": 28.066666666666666},
    }

    assert sarguardian_worker.resolve_target_coordinates(context, None, None) == (
        28.066666666666666,
        85.1,
    )


def test_regression_mode_keeps_the_established_compatibility_target_coordinates():
    context = {"aoi_mode": "langtang_regression", "centroid": {"lon": 0.0, "lat": 0.0}}

    assert sarguardian_worker.resolve_target_coordinates(context, None, None) == (28.27799, 85.52983)


@pytest.mark.parametrize(
    "aoi",
    [
        None,
        {"type": "Point", "coordinates": [85.0, 28.0]},
        {"type": "Polygon", "coordinates": [[[85.0, 28.0], [85.2, 28.0], [85.1, 28.2]]]},
        {"type": "Polygon", "coordinates": [[[85.0, 28.0], [85.0, 28.0], [85.0, 28.0], [85.0, 28.0]]]},
    ],
)
def test_runtime_aoi_rejects_invalid_or_missing_user_geometry(aoi):
    reader = _RuntimeAoiReader()

    with pytest.raises(RuntimeError, match="NISAR_GOFF_AOI_(INVALID|REQUIRED)"):
        sarguardian_worker.runtime_aoi_context({"aoi": aoi}, reader)


def test_langtang_is_available_only_as_explicit_regression_mode():
    reader = _RuntimeAoiReader()

    context = sarguardian_worker.runtime_aoi_context({"regression_mode": True}, reader)

    assert reader.selected == "source"
    assert context["aoi_mode"] == "langtang_regression"


def test_upload_metadata_inventory_is_complete_and_raw_h5_is_rejected(tmp_path):
    result_directory = tmp_path / "temporary-job-output"
    result_directory.mkdir()
    for name in drive_upload.EXPECTED_FILES:
        (result_directory / name).write_text("{}\n", encoding="utf-8")
    (result_directory / "manifest.json").write_text(
        json.dumps(
            {
                "science_commit": "77cc9646",
                "selected_products": ["goff-a", "goff-b"],
                "processing_parameters": {
                    "aoi": {"type": "Polygon", "coordinates": []},
                    "aoi_hash": "aoi-hash",
                    "aoi_centroid": {"lat": 28.1, "lon": 85.1},
                    "start_date": "2025-11-25",
                    "end_date": "2025-12-31",
                },
            }
        ),
        encoding="utf-8",
    )
    derived = result_directory / "derived" / "displacement.tif"
    derived.parent.mkdir()
    derived.write_bytes(b"derived scientific output")

    metadata = drive_upload.write_metadata(result_directory, "job-isolated")
    files = drive_upload.result_files(result_directory)

    assert metadata["job_id"] == "job-isolated"
    assert metadata["product_count"] == 2
    assert metadata["output_inventory"] == sorted(
        path.relative_to(result_directory).as_posix() for path in files
    )
    assert {
        path.relative_to(result_directory).as_posix() for path in files
    } == {*drive_upload.EXPECTED_FILES, "metadata.json", "derived/displacement.tif"}
    (result_directory / "raw-product.h5").write_bytes(b"not uploadable")
    with pytest.raises(drive_upload.DriveUploadError, match="DRIVE_RESULT_PACKAGE_CONTAINS_RAW_H5"):
        drive_upload.result_files(result_directory)


@pytest.mark.parametrize("relative_name", [
    "worker.log",
    "credentials.json",
    ".env.local",
    "oauth/token.json",
    "cache/value.bin",
    ".venv/site-packages/value.py",
    "result.hdf5",
])
def test_upload_rejects_non_scientific_or_sensitive_output_artifacts(tmp_path, relative_name):
    result_directory = tmp_path / "temporary-job-output"
    result_directory.mkdir()
    for name in drive_upload.EXPECTED_FILES:
        (result_directory / name).write_text("{}\n", encoding="utf-8")
    artifact = result_directory / relative_name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("must never be uploaded", encoding="utf-8")

    with pytest.raises(drive_upload.DriveUploadError):
        drive_upload.result_files(result_directory)


def test_drive_job_folders_are_isolated_by_job_id(monkeypatch):
    created = []

    monkeypatch.setattr(drive_upload, "list_run_folder_files", lambda *_args: [])
    monkeypatch.setattr(
        drive_upload,
        "create_run_folder",
        lambda _token, parent, name: created.append((parent, name)) or f"folder-{name}",
    )

    first = drive_upload.find_or_create_child_folder("token", "jobs-parent", "job-a")
    second = drive_upload.find_or_create_child_folder("token", "jobs-parent", "job-b")

    assert first != second
    assert created == [("jobs-parent", "job-a"), ("jobs-parent", "job-b")]
