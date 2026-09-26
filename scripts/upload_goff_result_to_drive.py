#!/usr/bin/env python3
"""Upload a packaged GOFF workflow result to the configured Google Drive."""

import argparse
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EXPECTED_ACCOUNT = "sarguardian.org@gmail.com"
EXPECTED_FILES = (
    "result.json",
    "timeseries.csv",
    "manifest.json",
    "README.txt",
)
METADATA_FILE = "metadata.json"
FORBIDDEN_OUTPUT_COMPONENTS = frozenset({
    ".cache", ".git", ".pytest_cache", ".venv", "__pycache__", "cache",
    "credentials", "google-oauth", "logs", "node_modules", "oauth", "venv",
})
FORBIDDEN_OUTPUT_FILENAMES = frozenset({
    ".env", ".netrc", "credentials.json", "id_rsa", "token.json",
})
FORBIDDEN_OUTPUT_SUFFIXES = frozenset({
    ".h5", ".hdf", ".hdf5", ".he5", ".key", ".log", ".pem", ".pyc", ".p12",
})
MIME_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
}
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"


class DriveUploadError(RuntimeError):
    """A deliberately non-sensitive Google Drive upload failure."""


class DriveRequestError(DriveUploadError):
    """A Drive request failure with optional safe HTTP status metadata."""

    def __init__(self, http_status=None):
        super().__init__("DRIVE_API_REQUEST_FAILED")
        self.http_status = http_status


class DriveFolderCreationError(DriveUploadError):
    """A folder-creation failure retaining a safe, stage-specific diagnostic."""

    def __init__(self, diagnostic):
        super().__init__("DRIVE_FOLDER_CREATION_FAILED")
        self.diagnostic = diagnostic


def required_environment(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")
    return value


def parse_client_configuration(raw_client_json):
    try:
        client_document = json.loads(raw_client_json)
    except json.JSONDecodeError as exc:
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID") from None

    if not isinstance(client_document, dict):
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")
    client = client_document.get("installed") or client_document.get("web")
    if client is None:
        client = client_document
    if not isinstance(client, dict):
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")

    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")
    if not client_id or not client_secret:
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")
    return client_id, client_secret


def parse_refresh_token(raw_secret):
    """Accept either a bare refresh token or the standard token.json object."""
    try:
        parsed = json.loads(raw_secret)
    except json.JSONDecodeError:
        token = raw_secret
    else:
        if isinstance(parsed, dict):
            token = parsed.get("refresh_token")
        elif isinstance(parsed, str):
            token = parsed
        else:
            token = None

    if not isinstance(token, str) or not token.strip():
        raise DriveUploadError("DRIVE_CONFIGURATION_INVALID")
    return token.strip()


def request_json(request, retries=0):
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=60) as response:
                response_body = response.read()
            break
        except HTTPError as exc:
            if attempt == retries:
                raise DriveRequestError(http_status=exc.code) from None
        except (URLError, OSError, TimeoutError):
            if attempt == retries:
                raise DriveRequestError() from None

    try:
        response_document = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        raise DriveUploadError("DRIVE_API_RESPONSE_INVALID") from None
    if not isinstance(response_document, dict):
        raise DriveUploadError("DRIVE_API_RESPONSE_INVALID")
    return response_document


def refresh_access_token(client_id, client_secret, refresh_token):
    payload = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    response = request_json(Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    ), retries=2)
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise DriveUploadError("DRIVE_AUTHENTICATION_FAILED")
    return access_token


def drive_request(access_token, url, method="GET", data=None, content_type=None):
    headers = {"Authorization": f"Bearer {access_token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return request_json(
        Request(url, data=data, headers=headers, method=method),
        retries=2 if method == "GET" else 0,
    )


def drive_url(query):
    return f"{DRIVE_API_URL}?{urlencode(query)}"


def folder_creation_diagnostic(stage, exc):
    if isinstance(exc, DriveRequestError):
        if exc.http_status is not None:
            return f"{stage}: http_status={exc.http_status}"
        return f"{stage}: network_or_timeout"
    return f"{stage}: response_invalid"


def create_run_folder(access_token, parent_folder_id, run_id):
    try:
        parent = drive_request(
            access_token,
            f"{DRIVE_API_URL}/{parent_folder_id}?{urlencode({'fields': 'id,mimeType'})}",
        )
    except DriveUploadError as exc:
        raise DriveFolderCreationError(
            folder_creation_diagnostic("DRIVE_PARENT_LOOKUP_FAILED", exc)
        ) from None
    print("DRIVE_PARENT_LOOKUP_SUCCEEDED")
    if parent.get("mimeType") != "application/vnd.google-apps.folder":
        raise DriveFolderCreationError("DRIVE_PARENT_FOLDER_INVALID")
    print("DRIVE_PARENT_FOLDER_CONFIRMED")
    metadata = json.dumps({
        "name": run_id,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }).encode("utf-8")
    print("DRIVE_FOLDER_CREATE_REQUEST_STARTED")
    try:
        response = drive_request(
            access_token,
            f"{DRIVE_API_URL}?{urlencode({'supportsAllDrives': 'true', 'fields': 'id'})}",
            method="POST",
            data=metadata,
            content_type="application/json; charset=UTF-8",
        )
    except DriveUploadError as exc:
        raise DriveFolderCreationError(
            folder_creation_diagnostic("DRIVE_FOLDER_CREATE_FAILED", exc)
        ) from None
    folder_id = response.get("id")
    if not isinstance(folder_id, str) or not folder_id:
        raise DriveFolderCreationError("DRIVE_FOLDER_CREATE_FAILED: response_invalid")
    return folder_id


def upload_file(access_token, folder_id, path, output_name=None):
    boundary = f"===============SARGuardian{uuid.uuid4().hex}"
    metadata = json.dumps({"name": output_name or path.name, "parents": [folder_id]}).encode("utf-8")
    mime_type = MIME_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"\r\n".join((
        f"--{boundary}".encode("ascii"),
        b"Content-Type: application/json; charset=UTF-8",
        b"",
        metadata,
        f"--{boundary}".encode("ascii"),
        f"Content-Type: {mime_type}".encode("ascii"),
        b"Content-Transfer-Encoding: binary",
        b"",
        path.read_bytes(),
        f"--{boundary}--".encode("ascii"),
        b"",
    ))
    response = drive_request(
        access_token,
        f"{DRIVE_UPLOAD_URL}?{urlencode({
            'uploadType': 'multipart',
            'supportsAllDrives': 'true',
            'fields': 'id',
        })}",
        method="POST",
        data=body,
        content_type=f"multipart/related; boundary={boundary}",
    )
    file_id = response.get("id")
    if not isinstance(file_id, str) or not file_id:
        raise DriveUploadError("DRIVE_FILE_UPLOAD_FAILED")
    return file_id


def escape_drive_query_value(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def verify_authenticated_account(access_token):
    response = drive_request(
        access_token,
        "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
    )
    user = response.get("user")
    email = user.get("emailAddress") if isinstance(user, dict) else None
    if not isinstance(email, str) or email.lower() != EXPECTED_ACCOUNT:
        raise DriveUploadError("DRIVE_ACCOUNT_VERIFICATION_FAILED")


def list_run_folder_files(access_token, folder_id):
    response = drive_request(access_token, drive_url({
        "q": f"'{escape_drive_query_value(folder_id)}' in parents and trashed = false",
        "fields": "files(id,name,mimeType)",
        "spaces": "drive",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }))
    files = response.get("files")
    if not isinstance(files, list):
        raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
    return files


def find_or_create_child_folder(access_token, parent_folder_id, name):
    matches = [
        item for item in list_run_folder_files(access_token, parent_folder_id)
        if item.get("name") == name
    ]
    folders = [
        item for item in matches
        if item.get("mimeType") == "application/vnd.google-apps.folder"
        and isinstance(item.get("id"), str)
    ]
    if any(item.get("mimeType") != "application/vnd.google-apps.folder" for item in matches):
        raise DriveUploadError("DRIVE_JOB_FOLDER_INVALID")
    if len(folders) > 1:
        raise DriveUploadError("DRIVE_DUPLICATE_JOB_FOLDERS")
    if folders:
        return folders[0]["id"]
    return create_run_folder(access_token, parent_folder_id, name)


def _safe_output_files(result_directory):
    """Return dynamic scientific outputs, rejecting artifacts that can leak secrets."""
    files = []
    for path in sorted(result_directory.rglob("*")):
        relative_path = path.relative_to(result_directory)
        parts = relative_path.parts
        normalized_parts = {part.lower() for part in parts}
        if normalized_parts & FORBIDDEN_OUTPUT_COMPONENTS:
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_CONTAINS_FORBIDDEN_ARTIFACT")
        if path.is_symlink():
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_CONTAINS_FORBIDDEN_ARTIFACT")
        if not path.is_file():
            continue
        file_name = path.name.lower()
        if (
            file_name in FORBIDDEN_OUTPUT_FILENAMES
            or file_name.startswith(".env")
            or path.suffix.lower() in FORBIDDEN_OUTPUT_SUFFIXES
        ):
            if path.suffix.lower() in {".h5", ".hdf", ".hdf5", ".he5"}:
                raise DriveUploadError("DRIVE_RESULT_PACKAGE_CONTAINS_RAW_H5")
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_CONTAINS_FORBIDDEN_ARTIFACT")
        files.append(path)
    return files


def result_files(result_directory):
    if not result_directory.is_dir():
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
    discovered_files = _safe_output_files(result_directory)
    files = []
    for name in EXPECTED_FILES:
        path = result_directory / name
        if not path.is_file():
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
        files.append(path)
    for path in discovered_files:
        if path.parent == result_directory and path.name in EXPECTED_FILES:
            continue
        files.append(path)
    relative_names = [path.relative_to(result_directory).as_posix() for path in files]
    if len(relative_names) != len(set(relative_names)):
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
    return files


def _json_document(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID") from None
    if not isinstance(document, dict):
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
    return document


def _runtime_aoi(manifest):
    parameters = manifest.get("processing_parameters")
    if not isinstance(parameters, dict):
        return None, None, None
    aoi = parameters.get("aoi")
    aoi_hash = parameters.get("aoi_hash")
    centroid = parameters.get("aoi_centroid")
    return aoi, aoi_hash, centroid


def write_metadata(result_directory, job_id):
    """Write non-sensitive provenance and an inventory before upload."""
    manifest = _json_document(result_directory / "manifest.json")
    aoi, aoi_hash, centroid = _runtime_aoi(manifest)
    parameters = manifest.get("processing_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    selected_products = manifest.get("selected_products")
    selected_products = selected_products if isinstance(selected_products, list) else []
    existing_files = [
        path.relative_to(result_directory).as_posix()
        for path in result_files(result_directory)
        if path.name != METADATA_FILE
    ]
    inventory = sorted(set(existing_files + [METADATA_FILE]))
    metadata = {
        "job_id": job_id,
        "aoi": aoi,
        "aoi_hash": aoi_hash,
        "centroid": centroid,
        "requested_dates": {
            "start_date": parameters.get("start_date"),
            "end_date": parameters.get("end_date"),
        },
        "selected_products": selected_products,
        "product_count": len(selected_products),
        "science_commit": manifest.get("science_commit"),
        "backend_commit": os.environ.get("GITHUB_SHA") or None,
        "processing_timestamp": datetime_now_utc(),
        "output_inventory": inventory,
        "warnings": [],
        "errors": [],
    }
    (result_directory / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def datetime_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def verify_result_manifest(result_directory):
    try:
        manifest = json.loads(
            (result_directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID") from None
    # The science worker writes a detailed manifest that is deliberately
    # distinct from result.json.  Checking its worker-specific completion
    # fields prevents an accidental result.json -> manifest.json copy from
    # being uploaded as a valid package.
    if (
        not isinstance(manifest, dict)
        or manifest.get("cleanup_status") != "success"
        or manifest.get("final_processing_status") != "completed"
    ):
        raise DriveUploadError("DRIVE_RAW_CLEANUP_NOT_CONFIRMED")


def serialized_file_ids(uploaded_file_ids):
    """Return the exact result-package filename-to-Drive-ID mapping."""
    if set(uploaded_file_ids) != set(EXPECTED_FILES):
        raise DriveUploadError("DRIVE_VERIFICATION_FAILED")

    ordered_file_ids = {}
    for name in EXPECTED_FILES:
        file_id = uploaded_file_ids.get(name)
        if not isinstance(file_id, str) or not file_id:
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        ordered_file_ids[name] = file_id
    return json.dumps(ordered_file_ids, separators=(",", ":"))


def serialized_output_file_ids(uploaded_file_ids):
    if not uploaded_file_ids or any(
        not isinstance(name, str) or not name or not isinstance(file_id, str) or not file_id
        for name, file_id in uploaded_file_ids.items()
    ):
        raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
    return json.dumps(dict(sorted(uploaded_file_ids.items())), separators=(",", ":"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Upload one temporary SARGuardian job package")
    parser.add_argument("--result-directory", default="result")
    if argv is None:
        argv = sys.argv[1:] if __name__ == "__main__" else []
    args = parser.parse_args(argv)
    try:
        client_id, client_secret = parse_client_configuration(
            required_environment("GOOGLE_DRIVE_OAUTH_CLIENT_JSON")
        )
        refresh_token = parse_refresh_token(
            required_environment("GOOGLE_DRIVE_REFRESH_TOKEN")
        )
        parent_folder_id = required_environment("GOOGLE_DRIVE_PARENT_FOLDER_ID")
        job_id = required_environment("JOB_ID")
        result_directory = Path(args.result_directory)
        verify_result_manifest(result_directory)
        metadata = write_metadata(result_directory, job_id)
        files = result_files(result_directory)
        inventory = [path.relative_to(result_directory).as_posix() for path in files]
        if sorted(inventory) != metadata["output_inventory"]:
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")

        print("DRIVE_UPLOAD_STARTED")
        print("DRIVE_RAW_H5_COUNT: 0")
        try:
            access_token = refresh_access_token(
                client_id, client_secret, refresh_token
            )
        except DriveUploadError:
            raise DriveUploadError("DRIVE_TOKEN_REFRESH_FAILED") from None
        try:
            verify_authenticated_account(access_token)
        except DriveUploadError:
            raise DriveUploadError("DRIVE_ACCOUNT_VERIFICATION_FAILED") from None
        try:
            jobs_folder_id = find_or_create_child_folder(
                access_token, parent_folder_id, "jobs"
            )
            folder_id = find_or_create_child_folder(access_token, jobs_folder_id, job_id)
        except DriveFolderCreationError:
            # Keep the specific, sanitized diagnostic for the outer handler,
            # which also emits the established high-level failure marker.
            raise
        except DriveUploadError:
            raise DriveUploadError("DRIVE_FOLDER_CREATION_FAILED") from None
        print(f"DRIVE_FOLDER_ID: {folder_id}")

        uploaded_file_ids = {}
        for path in files:
            relative_name = path.relative_to(result_directory).as_posix()
            uploaded_file_ids[relative_name] = upload_file(
                access_token, folder_id, path, output_name=relative_name
            )

        verified_files = list_run_folder_files(access_token, folder_id)
        verified_ids = {
            item.get("name"): item.get("id")
            for item in verified_files
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("id"), str)
        }
        verified_names = {item.get("name") for item in verified_files if isinstance(item, dict)}
        if len(verified_files) != len(files):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if verified_names != set(inventory):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if any(name.lower().endswith(".h5") for name in verified_names):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if any(verified_ids.get(name) != uploaded_file_ids[name] for name in inventory):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        print("DRIVE_UPLOAD_VERIFIED")
        print(f"DRIVE_FILE_IDS_JSON: {serialized_output_file_ids(uploaded_file_ids)}")
    except DriveUploadError as exc:
        if isinstance(exc, DriveFolderCreationError):
            print(exc.diagnostic)
        print(str(exc))
        print("DRIVE_UPLOAD_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
