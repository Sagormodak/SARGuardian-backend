import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


class WorkerDispatchError(RuntimeError):
    """Raised when a GitHub Actions worker cannot be dispatched."""


def dispatch_job(job_id: str, parameters: dict) -> None:
    if not settings.github_actions_token:
        raise WorkerDispatchError("Worker dispatch is not configured")

    payload = json.dumps(
        {
            "ref": "main",
            "inputs": {
                "job_id": job_id,
                "processing_metadata": json.dumps(
                    parameters,
                    separators=(",", ":"),
                ),
            },
        }
    ).encode("utf-8")

    url = (
        "https://api.github.com/repos/"
        f"{settings.github_actions_repository}/actions/workflows/"
        f"{settings.github_actions_workflow}/dispatches"
    )

    request = Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.github_actions_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "SARGuardian-worker-dispatch",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            if response.status not in (200, 201, 202, 204):
                raise WorkerDispatchError("Worker dispatch failed")
    except HTTPError as exc:
        raise WorkerDispatchError("Worker dispatch failed") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise WorkerDispatchError("Worker dispatch failed") from exc
