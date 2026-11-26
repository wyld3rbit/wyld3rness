"""The credential: a permission for one action, signed outside the agent, checked by the service that runs it.

    loop ── sign(action) ──► Signer (OpenBao's Transit engine: the key never leaves it)
         ◄── token ────────┘
    loop ── apply(action, token) ──► environment ──► service: genuine? this action? not expired? not used?

    token   = base64url(payload) "." signature
    payload = canonical JSON {"action", "args", "exp", "jti"}: one action with its exact
              arguments, an expiry `ttl_s` from now (Unix seconds), and a one-time id

Why one action and not a session: a token that is stolen, logged or replayed can
do exactly one thing, once, within a minute. Why the signer is outside the
agent: the agent never holds anything that can make a token, so a fully fooled
model cannot authorise itself. Why the service checks it and not the harness:
the check must sit where the action runs, after anything that could change the
request on the way (Day 7 lesson, step 4).

The format is a protocol between two codebases: this module writes it and the
service that checks it (simprod's `credentials.py`, standard library only, in a
container that has no harness) reads it. simprod's tests round-trip a token
made here through its check, so the two sides cannot drift apart unnoticed.

Day 8 revisits the lifetime and the one-time ids when several agents act at
once: a shared store of used ids replaces each service's memory.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
import time
from typing import Protocol
import urllib.request
import uuid

from wyld3rness.loop.env import Action

TTL_S = 60  # a token is good for this long after it is signed: enough to reach the service, not to be kept
KEY = "actions"  # the Transit key's name


class Signer(Protocol):
    """Makes the token for one action. Raises when it cannot; the loop then does not send the action."""

    def sign(self, action: Action) -> str:
        """The token that permits exactly `action`, once, for a short time."""
        ...


def payload(action: Action, exp: int, jti: str) -> bytes:
    """The bytes that are signed: canonical JSON, so the same claims always give the same bytes. Pure."""
    claims = {"action": action.name, "args": dict(action.args), "exp": exp, "jti": jti}
    return json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()


def token(signed: bytes, signature: str) -> str:
    """The token for `signed` bytes and their signature. Pure."""
    return base64.urlsafe_b64encode(signed).decode().rstrip("=") + "." + signature


Post = Callable[[str, dict[str, object], str], dict[str, object]]  # (url, body, vault token) -> response JSON


def _post(url: str, body: dict[str, object], vault_token: str) -> dict[str, object]:
    """One authenticated call to the vault's HTTP API. Raises on any failure."""
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"X-Vault-Token": vault_token, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        answer: dict[str, object] = json.load(r)
        return answer


class TransitSigner:
    """A `Signer` backed by OpenBao's Transit engine: OpenBao signs, and the key never leaves it.

    `vault_token` is a pass that may only call this key's sign endpoint; the
    app reads it from where its vault set-up wrote it and never prints it.
    """

    def __init__(
        self,
        addr: str,
        vault_token: str,
        *,
        key: str = KEY,
        ttl_s: int = TTL_S,
        clock: Callable[[], float] = time.time,
        post: Post = _post,
    ) -> None:
        """`addr` is the vault's base URL, such as http://127.0.0.1:8200."""
        self.url = f"{addr.rstrip('/')}/v1/transit/sign/{key}"
        self._vault_token, self.ttl_s, self.clock, self._post = vault_token, ttl_s, clock, post

    def sign(self, action: Action) -> str:
        """Sign one action's claims; raises if the vault refuses or cannot be reached."""
        signed = payload(action, int(self.clock()) + self.ttl_s, uuid.uuid4().hex)
        answer = self._post(self.url, {"input": base64.b64encode(signed).decode()}, self._vault_token)
        data = answer.get("data")
        signature = data.get("signature") if isinstance(data, dict) else None
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("the vault returned no signature")
        return token(signed, signature)
