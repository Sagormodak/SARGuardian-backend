import json
import os
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
REQUIRED_SCOPE = "https://www.googleapis.com/auth/drive"


class DriveClientError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


class DriveClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token: str | None = None

    @classmethod
    def from_environment(cls) -> "DriveClient":
        raw_client = os.environ.get("GOOGLE_DRIVE_OAUTH_CLIENT_JSON", "").strip()
        raw_refresh = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()
        if not raw_client or not raw_refresh:
            raise DriveClientError("DRIVE_CONFIGURATION_INVALID")
        try:
            document = json.loads(raw_client)
            client = document.get("installed") or document.get("web") or document
            parsed_refresh = json.loads(raw_refresh)
            refresh_token = (
                parsed_refresh.get("refresh_token")
                if isinstance(parsed_refresh, dict)
                else parsed_refresh if isinstance(parsed_refresh, str) else raw_refresh
            )
        except (TypeError, json.JSONDecodeError):
            raise DriveClientError("DRIVE_CONFIGURATION_INVALID") from None
        if not isinstance(client, dict):
            raise DriveClientError("DRIVE_CONFIGURATION_INVALID")
        client_id = client.get("client_id")
        client_secret = client.get("client_secret")
        if not all(isinstance(value, str) and value for value in (client_id, client_secret, refresh_token)):
            raise DriveClientError("DRIVE_CONFIGURATION_INVALID")
        return cls(client_id, client_secret, refresh_token.strip())

    def refresh_access_token(self) -> int:
        payload = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }).encode("utf-8")
        response = self._request_json(
            Request(
                TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            ),
            "DRIVE_TOKEN_REFRESH_FAILED",
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise DriveClientError("DRIVE_TOKEN_REFRESH_FAILED")
        self.access_token = token
        return 200

    def _authorized_request(self, url: str, method: str = "GET", data: bytes | None = None, content_type: str | None = None) -> Request:
        if self.access_token is None:
            self.refresh_access_token()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return Request(url, data=data, headers=headers, method=method)

    def _request_json(self, request: Request, error_code: str, retries: int = 0) -> dict:
        for attempt in range(retries + 1):
            try:
                with urlopen(request, timeout=60) as response:
                    document = json.loads(response.read())
                if not isinstance(document, dict):
                    raise DriveClientError("DRIVE_API_RESPONSE_INVALID")
                return document
            except HTTPError as exc:
                if attempt == retries:
                    raise DriveClientError(error_code, exc.code) from None
            except (URLError, OSError, TimeoutError, json.JSONDecodeError):
                if attempt == retries:
                    raise DriveClientError(error_code) from None
        raise DriveClientError(error_code)

    def about_email(self) -> tuple[int, str | None]:
        response = self._request_json(
            self._authorized_request("https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"),
            "DRIVE_ACCOUNT_LOOKUP_FAILED",
            retries=2,
        )
        user = response.get("user")
        email = user.get("emailAddress") if isinstance(user, dict) else None
        return 200, email if isinstance(email, str) else None

    def get_file(self, file_id: str) -> dict:
        return self._request_json(
            self._authorized_request(
                f"{DRIVE_API_URL}/{file_id}?{urlencode({'fields': 'id,name,mimeType,parents'})}"
            ),
            "DRIVE_FILE_LOOKUP_FAILED",
            retries=2,
        )

    def list_children(self, parent_id: str) -> list[dict]:
        query = {
            "q": f"'{self._escape(parent_id)}' in parents and trashed = false",
            "fields": "files(id,name,mimeType,parents)",
            "spaces": "drive",
        }
        response = self._request_json(
            self._authorized_request(
                f"{DRIVE_API_URL}?{urlencode(query)}"
            ),
            "DRIVE_LIST_FAILED",
            retries=2,
        )
        files = response.get("files")
        if not isinstance(files, list):
            raise DriveClientError("DRIVE_LIST_FAILED")
        return [file for file in files if isinstance(file, dict)]

    def create_folder(self, parent_id: str, name: str) -> str:
        metadata = json.dumps({"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [parent_id]}).encode("utf-8")
        response = self._request_json(
            self._authorized_request(
                f"{DRIVE_API_URL}?{urlencode({'supportsAllDrives': 'true', 'fields': 'id'})}",
                method="POST",
                data=metadata,
                content_type="application/json; charset=UTF-8",
            ),
            "DRIVE_FOLDER_CREATE_FAILED",
        )
        folder_id = response.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise DriveClientError("DRIVE_FOLDER_CREATE_FAILED")
        return folder_id

    def upload_file(self, parent_id: str, path: Path, mime_type: str) -> str:
        boundary = f"SARGuardian{uuid.uuid4().hex}"
        metadata = json.dumps({"name": path.name, "parents": [parent_id]}).encode("utf-8")
        body = b"\r\n".join((
            f"--{boundary}".encode("ascii"),
            b"Content-Type: application/json; charset=UTF-8", b"", metadata,
            f"--{boundary}".encode("ascii"),
            f"Content-Type: {mime_type}".encode("ascii"),
            b"Content-Transfer-Encoding: binary", b"", path.read_bytes(),
            f"--{boundary}--".encode("ascii"), b"",
        ))
        response = self._request_json(
            self._authorized_request(
                f"{DRIVE_UPLOAD_URL}?{urlencode({'uploadType': 'multipart', 'supportsAllDrives': 'true', 'fields': 'id'})}",
                method="POST",
                data=body,
                content_type=f"multipart/related; boundary={boundary}",
            ),
            "DRIVE_FILE_UPLOAD_FAILED",
        )
        file_id = response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise DriveClientError("DRIVE_FILE_UPLOAD_FAILED")
        return file_id

    def download_file(self, file_id: str) -> bytes:
        request = self._authorized_request(
            f"{DRIVE_API_URL}/{file_id}?{urlencode({'alt': 'media'})}"
        )
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except (HTTPError, URLError, OSError, TimeoutError):
            raise DriveClientError("DRIVE_FILE_DOWNLOAD_FAILED") from None

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")
