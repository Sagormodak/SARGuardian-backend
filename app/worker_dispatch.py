"""GitHub Actions worker dispatch for SARGuardian."""

import json
import os
import subprocess
from typing import Any

import httpx

from app.config import settings


class DispatchError(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def workflow_inputs(
    job_id: str,
    parameters: dict[str, Any],
    benchmark_only: bool = False,
) -> dict[str, str]:
    """Build a safe workflow contract for either a user AOI or regression run."""
    aoi = parameters.get("aoi")
    regression_mode = parameters.get("regression_mode") is True
    if aoi is None and not regression_mode:
        raise DispatchError("DISPATCH_AOI_REQUIRED", "A user AOI is required for production science")
    if aoi is not None and not isinstance(aoi, dict):
        raise DispatchError("DISPATCH_AOI_INVALID", "User AOI must be a GeoJSON object")
    try:
        aoi_geojson = json.dumps(aoi, separators=(",", ":")) if aoi is not None else ""
    except (TypeError, ValueError):
        raise DispatchError("DISPATCH_AOI_INVALID", "User AOI must be valid JSON") from None
    if len(aoi_geojson.encode("utf-8")) > 60_000:
        raise DispatchError("DISPATCH_AOI_INVALID", "User AOI is too large for workflow dispatch")

    def optional_input(name: str) -> str:
        value = parameters.get(name)
        return "" if value is None else str(value)

    return {
        "job_id": job_id,
        "benchmark_only": str(benchmark_only).lower(),
        "target_lat": optional_input("target_lat"),
        "target_lon": optional_input("target_lon"),
        "start_date": str(parameters.get("start_date", "2025-11-25")),
        "end_date": optional_input("end_date"),
        "aoi_geojson": aoi_geojson,
        "regression_mode": str(regression_mode).lower(),
    }


def dispatch_to_github_actions(
    job_id: str,
    parameters: dict[str, Any],
    benchmark_only: bool = False,
) -> str:
    """
    Dispatch a job to the SARGuardian GitHub Actions workflow.

    Returns the GitHub Actions run ID.
    """
    github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "Sagormodak/SARGuardian-backend")
    workflow_file = "sarguardian-science-worker.yml"
    workflow_ref = settings.github_workflow_ref

    if not github_token:
        raise DispatchError(
            "DISPATCH_CONFIGURATION_INVALID",
            "GitHub token not configured for workflow dispatch",
        )

    inputs = workflow_inputs(job_id, parameters, benchmark_only)

    url = f"https://api.github.com/repos/{github_repository}/actions/workflows/{workflow_file}/dispatches"
    payload = {
        "ref": workflow_ref,
        "inputs": inputs,
    }

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)
            if response.status_code == 204:
                pass
            elif response.status_code == 404:
                raise DispatchError(
                    "DISPATCH_WORKFLOW_NOT_FOUND",
                    "GitHub Actions workflow not found",
                )
            elif response.status_code == 401:
                raise DispatchError(
                    "DISPATCH_UNAUTHORIZED",
                    "GitHub token invalid or insufficient permissions",
                )
            elif response.status_code == 422:
                raise DispatchError(
                    "DISPATCH_INVALID_INPUTS",
                    "Workflow dispatch inputs are invalid",
                )
            else:
                raise DispatchError(
                    "DISPATCH_FAILED",
                    f"Workflow dispatch failed: {response.status_code}",
                )
    except httpx.RequestError as exc:
        raise DispatchError(
            "DISPATCH_NETWORK_ERROR",
            "Failed to contact GitHub API",
        ) from exc

    return "dispatched"


def dispatch_via_gh_cli(
    job_id: str,
    parameters: dict[str, Any],
    benchmark_only: bool = False,
) -> str:
    """
    Dispatch using gh CLI as fallback.

    Requires gh to be installed and authenticated.
    """
    github_repository = os.environ.get("GITHUB_REPOSITORY", "Sagormodak/SARGuardian-backend")
    workflow_file = "sarguardian-science-worker.yml"
    workflow_ref = settings.github_workflow_ref

    inputs = workflow_inputs(job_id, parameters, benchmark_only)
    cmd = [
        "gh", "workflow", "run", workflow_file,
        "--repo", github_repository,
        "--ref", workflow_ref,
    ]
    for name, value in inputs.items():
        cmd.extend(("-f", f"{name}={value}"))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise DispatchError(
            "DISPATCH_CLI_FAILED",
            f"gh CLI dispatch failed: {exc.stderr}",
        ) from exc
    except FileNotFoundError:
        raise DispatchError(
            "DISPATCH_CLI_NOT_FOUND",
            "gh CLI not available",
        ) from None


def dispatch_job(
    job_id: str,
    parameters: dict[str, Any],
    benchmark_only: bool = False,
) -> str:
    """
    Dispatch a job to GitHub Actions, trying API first then CLI.
    """
    # Validate before choosing a transport so invalid production jobs never
    # fall through to the CLI or start a runner.
    workflow_inputs(job_id, parameters, benchmark_only)
    try:
        return dispatch_to_github_actions(job_id, parameters, benchmark_only)
    except DispatchError:
        return dispatch_via_gh_cli(job_id, parameters, benchmark_only)
