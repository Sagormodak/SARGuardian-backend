import socket

import pytest

from scripts import sarguardian_worker as worker


class FakeEarthaccess:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.strategies = []

    def login(self, *, strategy):
        self.calls += 1
        self.strategies.append(strategy)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def no_preflight():
    return None


def test_earthdata_login_succeeds_on_the_first_attempt(capsys):
    auth = object()
    earthaccess = FakeEarthaccess([auth])

    returned = worker.login_earthdata_with_retry(
        earthaccess, preflight=no_preflight, delays=(0, 0, 0)
    )

    assert returned is auth
    assert earthaccess.calls == 1
    assert earthaccess.strategies == ["environment"]
    assert "EARTHDATA_LOGIN_ATTEMPT: 1/4" in capsys.readouterr().out


def test_earthdata_login_retries_a_timeout_then_succeeds(capsys):
    auth = object()
    earthaccess = FakeEarthaccess([TimeoutError("Read timed out"), auth])
    delays = []

    returned = worker.login_earthdata_with_retry(
        earthaccess,
        preflight=no_preflight,
        sleep_fn=delays.append,
        delays=(5, 15, 30),
    )

    assert returned is auth
    assert earthaccess.calls == 2
    assert delays == [5]
    output = capsys.readouterr().out
    assert "EARTHDATA_LOGIN_SERVICE_TIMEOUT: attempt=1" in output
    assert "EARTHDATA_AUTH_SUCCESS: attempt=2" in output


def test_earthdata_login_reports_a_safe_code_after_repeated_timeouts(capsys):
    earthaccess = FakeEarthaccess([TimeoutError("Read timed out")] * 4)
    delays = []

    with pytest.raises(
        worker.EarthdataLoginError,
        match="^NISAR_GOFF_EARTHDATA_SERVICE_TIMEOUT$",
    ):
        worker.login_earthdata_with_retry(
            earthaccess,
            preflight=no_preflight,
            sleep_fn=delays.append,
            delays=(5, 15, 30),
        )

    assert earthaccess.calls == 4
    assert delays == [5, 15, 30]
    assert "EARTHDATA_LOGIN_SERVICE_TIMEOUT: attempt=4" in capsys.readouterr().out


def test_earthdata_login_does_not_retry_an_authentication_rejection(capsys):
    earthaccess = FakeEarthaccess([RuntimeError("401 Unauthorized")])

    with pytest.raises(
        worker.EarthdataLoginError,
        match="^NISAR_GOFF_EARTHDATA_AUTH_FAILED$",
    ):
        worker.login_earthdata_with_retry(
            earthaccess, preflight=no_preflight, delays=(0, 0, 0)
        )

    assert earthaccess.calls == 1
    assert "EARTHDATA_LOGIN_AUTH_REJECTED: attempt=1" in capsys.readouterr().out


def test_earthdata_login_never_leaks_a_secret_in_logs_or_safe_errors(capsys):
    secret = "do-not-log-earthdata-password"
    earthaccess = FakeEarthaccess([TimeoutError(f"Read timed out: {secret}")] * 2)

    with pytest.raises(worker.EarthdataLoginError) as error:
        worker.login_earthdata_with_retry(
            earthaccess,
            preflight=no_preflight,
            sleep_fn=lambda _delay: None,
            delays=(0,),
        )

    output = capsys.readouterr().out
    assert secret not in output
    assert secret not in str(error.value)
    assert str(error.value) == "NISAR_GOFF_EARTHDATA_SERVICE_TIMEOUT"


def test_preflight_reports_dns_network_unavailable_without_exposing_details(capsys):
    secret = "do-not-log-dns-detail"

    def unavailable_dns(*_args, **_kwargs):
        raise socket.gaierror(f"name resolution failed: {secret}")

    with pytest.raises(
        worker.EarthdataLoginError,
        match="^NISAR_GOFF_EARTHDATA_NETWORK_UNAVAILABLE$",
    ):
        worker.preflight_earthdata_endpoint(
            resolver=unavailable_dns,
            opener=lambda *_args, **_kwargs: object(),
            sleep_fn=lambda _delay: None,
            delays=(0,),
        )

    output = capsys.readouterr().out
    assert "EARTHDATA_PREFLIGHT_DNS_NETWORK_UNAVAILABLE" in output
    assert secret not in output
