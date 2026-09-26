import pytest

from app.worker_dispatch import DispatchError, dispatch_job, workflow_inputs


USER_AOI = {
    "type": "Polygon",
    "coordinates": [[[85.0, 28.0], [85.2, 28.0], [85.1, 28.2], [85.0, 28.0]]],
}


def test_workflow_inputs_pass_a_user_aoi_and_leave_legacy_target_coordinates_empty():
    inputs = workflow_inputs("job-1", {"aoi": USER_AOI, "start_date": "2026-01-01"})

    assert inputs["aoi_geojson"] == (
        '{"type":"Polygon","coordinates":[[[85.0,28.0],[85.2,28.0],[85.1,28.2],[85.0,28.0]]]}'
    )
    assert inputs["target_lat"] == ""
    assert inputs["target_lon"] == ""
    assert inputs["regression_mode"] == "false"


def test_workflow_inputs_require_user_aoi_except_for_explicit_regression_mode():
    with pytest.raises(DispatchError) as exc_info:
        workflow_inputs("job-1", {})
    assert exc_info.value.code == "DISPATCH_AOI_REQUIRED"

    inputs = workflow_inputs("job-1", {"regression_mode": True})
    assert inputs["aoi_geojson"] == ""
    assert inputs["regression_mode"] == "true"


def test_invalid_aoi_is_rejected_before_dispatch_transport(monkeypatch):
    monkeypatch.setattr(
        "app.worker_dispatch.dispatch_to_github_actions",
        lambda *_args: pytest.fail("API dispatch must not be attempted"),
    )
    monkeypatch.setattr(
        "app.worker_dispatch.dispatch_via_gh_cli",
        lambda *_args: pytest.fail("CLI dispatch must not be attempted"),
    )

    with pytest.raises(DispatchError) as exc_info:
        dispatch_job("job-1", {})
    assert exc_info.value.code == "DISPATCH_AOI_REQUIRED"
