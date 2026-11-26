"""Tests for simprod/credentials.py: the guard every action passes, and the format shared with the harness."""

import json

import pytest

from simprod import vault
from simprod.credentials import Guard, action_args, decode
from simprod.service import Response, Route, Service
from wyld3rness.loop.credential import TransitSigner
from wyld3rness.loop.env import Action

NOW = 1_000_000.0


def harness_token(action: Action, ttl_s: int = 60, signature: str = "good") -> str:
    """A token made by the harness's own signer, with a fake vault that signs everything `signature`."""
    return TransitSigner(
        "http://bao", "p", ttl_s=ttl_s, clock=lambda: NOW, post=lambda u, b, t: {"data": {"signature": signature}}
    ).sign(action)


def guard(genuine: bool = True) -> Guard:
    """A guard whose vault says every signature is `genuine` (or not)."""
    return Guard(lambda signed, signature: genuine and signature == "good", clock=lambda: NOW)


CAP = Action("rate_cap", {"key": "acct-0640", "rps": 10.0})


def test_a_harness_token_permits_exactly_its_action_once() -> None:
    g, t = guard(), harness_token(CAP)
    jti, refused = g.check(t, "rate_cap", {"key": "acct-0640", "rps": 10})  # 10 and 10.0 are the same number
    assert refused is None and jti
    assert g.check(t, "rate_cap", {"key": "acct-0640", "rps": 10}) == (None, "credential was already used")


@pytest.mark.parametrize(
    ("header", "action", "args", "reason"),
    [
        (None, "rate_cap", CAP.args, "no credential"),
        ("not-a-token", "rate_cap", CAP.args, "malformed credential"),
        (harness_token(CAP, signature="forged"), "rate_cap", CAP.args, "not genuine"),
        (harness_token(CAP), "rate_cap", {"key": "acct-0001", "rps": 10.0}, "credential is for rate_cap"),
        (harness_token(CAP), "backoff", {"enabled": True}, "credential is for rate_cap"),
        (harness_token(CAP, ttl_s=-1), "rate_cap", CAP.args, "expired"),
        (harness_token(CAP, ttl_s=3600), "rate_cap", CAP.args, "longer than 120 s"),
    ],
)
def test_a_credential_is_refused_with_its_reason(header, action, args, reason) -> None:
    jti, refused = guard().check(header, action, args)
    assert jti is None and reason in str(refused)


def test_a_vault_that_cannot_be_reached_refuses_every_action() -> None:
    def down(signed: bytes, signature: str) -> bool:
        raise ConnectionError("vault down")

    _, refused = Guard(down, clock=lambda: NOW).check(harness_token(CAP), "rate_cap", CAP.args)
    assert refused == "credential could not be verified: ConnectionError: vault down"


def test_a_restarts_argument_is_the_service_itself() -> None:
    assert action_args("ledger", "/actions/restart", {}) == ("restart", {"service": "ledger"})
    assert action_args("gateway", "/actions/backoff", {"enabled": True}) == ("backoff", {"enabled": True})
    assert action_args("gateway", "/pay", {}) is None


def test_the_edge_path_names_the_same_action_the_guard_checks() -> None:
    assert vault.action_of("/gateway/actions/rate-cap", {"key": "acct-0640", "rps": 10.0}) == CAP
    assert vault.action_of("/ledger/actions/restart", {}) == Action("restart", {"service": "ledger"})
    with pytest.raises(ValueError):
        vault.action_of("/gateway/pay", {})


def test_decode_reads_what_the_harness_writes() -> None:
    _, signature, claims = decode(harness_token(CAP))
    assert signature == "good" and claims["action"] == "rate_cap" and claims["exp"] == NOW + 60


def _service(g: Guard) -> tuple[Service, list[int]]:
    """A service with one action, and the list its handler appends to on every call."""
    calls: list[int] = []

    def handle(req) -> Response:
        calls.append(1)
        return Response(200, {"done": True})

    return Service("gateway", [Route("POST", "/actions/backoff", handle, "Backoff", action=True)], guard=g), calls


def test_the_service_runs_an_action_only_with_a_valid_credential() -> None:
    svc, calls = _service(guard())
    body = json.dumps({"enabled": True}).encode()
    refused = svc.dispatch("POST", "/actions/backoff", body, {})
    assert refused.status == 403 and refused.body["reason"] == "no credential" and calls == []
    ok = svc.dispatch(
        "POST", "/actions/backoff", body, {"x-action-token": harness_token(Action("backoff", {"enabled": True}))}
    )
    assert ok.status == 200 and calls == [1]


def test_the_spec_says_an_action_needs_a_credential() -> None:
    op = _service(guard())[0].openapi()["paths"]["/actions/backoff"]["post"]
    assert op["parameters"][0]["name"] == "X-Action-Token" and op["parameters"][0]["required"] is True
    assert "403" in op["responses"]


def test_every_service_with_actions_is_deployed_with_its_guard() -> None:
    """guard_from_env returns None when unset; the compose file must set it wherever an action runs."""
    from pathlib import Path
    import re

    compose = (Path(vault.__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    for service in ("ledger", "notifications", "gateway"):
        # The service's block: from its name to the next line at the same two-space indent.
        block = re.search(rf"\n  {service}:\n(.*?)(?=\n  [a-z#]|\Z)", compose, re.S)
        assert block, service
        assert "BAO_ADDR: http://openbao:8200" in block.group(1), service
        assert "/run/bao/verify.token:ro" in block.group(1), service
