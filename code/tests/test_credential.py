"""Tests for wyld3rness/loop/credential.py: the claims that are signed and the OpenBao signer."""

import base64
import json

import pytest

from wyld3rness.loop.credential import TTL_S, TransitSigner, payload, token
from wyld3rness.loop.env import Action


def test_the_signed_bytes_are_canonical_json_of_one_action() -> None:
    signed = payload(Action("rate_cap", {"rps": 10.0, "key": "acct-0640"}), 1000, "abc")
    assert signed == b'{"action":"rate_cap","args":{"key":"acct-0640","rps":10.0},"exp":1000,"jti":"abc"}'


def test_the_token_is_the_payload_then_the_signature() -> None:
    t = token(b'{"a":1}', "vault:v1:c2ln")
    head, _, signature = t.partition(".")
    assert base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)) == b'{"a":1}' and signature == "vault:v1:c2ln"


def test_the_signer_asks_the_vault_to_sign_the_claims_with_its_pass() -> None:
    calls = []

    def post(url: str, body: dict, vault_token: str) -> dict:
        """The vault: record the call, return a signature."""
        calls.append((url, body, vault_token))
        return {"data": {"signature": "vault:v1:c2ln"}}

    signer = TransitSigner("http://bao:8200/", "pass-1", clock=lambda: 1000.0, post=post)
    t = signer.sign(Action("backoff", {"enabled": True}))
    url, body, pass_ = calls[0]
    claims = json.loads(base64.b64decode(body["input"]))
    assert url == "http://bao:8200/v1/transit/sign/actions" and pass_ == "pass-1"
    assert claims["action"] == "backoff" and claims["args"] == {"enabled": True} and claims["exp"] == 1000 + TTL_S
    assert t.endswith(".vault:v1:c2ln")


def test_each_token_has_its_own_one_time_id() -> None:
    def post(url: str, body: dict, vault_token: str) -> dict:
        return {"data": {"signature": "s"}}

    signer = TransitSigner("http://bao:8200", "p", post=post)
    a, b = (signer.sign(Action("backoff", {"enabled": True})) for _ in range(2))
    assert a != b


def test_a_vault_that_returns_no_signature_raises() -> None:
    signer = TransitSigner("http://bao:8200", "p", post=lambda url, body, t: {"errors": ["permission denied"]})
    with pytest.raises(RuntimeError, match="no signature"):
        signer.sign(Action("backoff", {"enabled": True}))
