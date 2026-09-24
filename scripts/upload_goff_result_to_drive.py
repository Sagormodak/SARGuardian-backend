#!/usr/bin/env python3
"""Upload a packaged GOFF workflow result to the configured Google Drive."""

import json
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
        except (HTTPError, URLError, OSError, TimeoutError):
            if attempt == retries:
                raise DriveUploadError("DRIVE_API_REQUEST_FAILED") from None

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


def create_run_folder(access_token, parent_folder_id, run_id):
    parent = drive_request(
        access_token,
        f"{DRIVE_API_URL}/{parent_folder_id}?{urlencode({'fields': 'id,mimeType'})}",
    )
    if parent.get("mimeType") != "application/vnd.google-apps.folder":
        raise DriveUploadError("DRIVE_PARENT_FOLDER_INVALID")
    metadata = json.dumps({
        "name": run_id,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }).encode("utf-8")
    response = drive_request(
        access_token,
        f"{DRIVE_API_URL}?{urlencode({'supportsAllDrives': 'true', 'fields': 'id'})}",
        method="POST",
        data=metadata,
        content_type="application/json; charset=UTF-8",
    )
    folder_id = response.get("id")
    if not isinstance(folder_id, str) or not folder_id:
        raise DriveUploadError("DRIVE_FOLDER_CREATION_FAILED")
    return folder_id


def upload_file(access_token, folder_id, path):
    boundary = f"===============SARGuardian{uuid.uuid4().hex}"
    metadata = json.dumps({"name": path.name, "parents": [folder_id]}).encode("utf-8")
    mime_type = MIME_TYPES[path.suffix.lower()]
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


def result_files(result_directory):
    if not result_directory.is_dir():
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
    raw_h5_count = sum(
        path.is_file() and path.suffix.lower() == ".h5"
        for path in result_directory.rglob("*")
    )
    if raw_h5_count != 0:
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_CONTAINS_RAW_H5")
    files = []
    for name in EXPECTED_FILES:
        path = result_directory / name
        if not path.is_file():
            raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID")
        files.append(path)
    return files


def verify_result_manifest(result_directory):
    try:
        manifest = json.loads(
            (result_directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        raise DriveUploadError("DRIVE_RESULT_PACKAGE_INVALID") from None
    if not isinstance(manifest, dict) or not manifest.get("raw_cleanup_success"):
        raise DriveUploadError("DRIVE_RAW_CLEANUP_NOT_CONFIRMED")


def main():
    try:
        client_id, client_secret = parse_client_configuration(
            required_environment("GOOGLE_DRIVE_OAUTH_CLIENT_JSON")
        )
        refresh_token = parse_refresh_token(
            required_environment("GOOGLE_DRIVE_REFRESH_TOKEN")
        )
        parent_folder_id = required_environment("GOOGLE_DRIVE_PARENT_FOLDER_ID")
        run_id = required_environment("GITHUB_RUN_ID")
        result_directory = Path("result")
        files = result_files(result_directory)
        verify_result_manifest(result_directory)

        print("DRIVE_UPLOAD_STARTED")
        print("DRIVE_RAW_H5_COUNT: 0")
        access_token = refresh_access_token(client_id, client_secret, refresh_token)
        verify_authenticated_account(access_token)
        folder_id = create_run_folder(access_token, parent_folder_id, run_id)
        print(f"DRIVE_FOLDER_ID: {folder_id}")

        uploaded_file_ids = {}
        for path in files:
            uploaded_file_ids[path.name] = upload_file(access_token, folder_id, path)
            print(f"DRIVE_FILE_ID: {uploaded_file_ids[path.name]}")

        verified_files = list_run_folder_files(access_token, folder_id)
        verified_ids = {
            item.get("name"): item.get("id")
            for item in verified_files
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("id"), str)
        }
        verified_names = {item.get("name") for item in verified_files if isinstance(item, dict)}
        if len(verified_files) != len(EXPECTED_FILES):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if verified_names != set(EXPECTED_FILES):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if any(name.lower().endswith(".h5") for name in verified_names):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        if any(verified_ids.get(name) != uploaded_file_ids[name] for name in EXPECTED_FILES):
            raise DriveUploadError("DRIVE_VERIFICATION_FAILED")
        print("DRIVE_UPLOAD_VERIFIED")
    except DriveUploadError as exc:
        print(str(exc))
        print("DRIVE_UPLOAD_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
