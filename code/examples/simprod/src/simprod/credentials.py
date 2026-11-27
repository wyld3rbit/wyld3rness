"""The guard: every action a service runs must carry a credential for exactly that action, checked here first.

    X-Action-Token: base64url(payload) "." signature
        ├─ the signature, verified by OpenBao (POST /v1/transit/verify/actions): the key never leaves it
        ├─ the action and its arguments, equal to what this request asks for
        ├─ not expired, and not good for longer than MAX_TTL_S
        └─ its one-time id not seen before by this service
    any check that fails, or cannot be made ─► 403 with the reason, logged; the action does not run

The token format is written by the harness (`wyld3rness.loop.credential`) and
read here. This module cannot import the harness: it runs in the services'
image, which holds this folder's code and the standard library only. The
tests round-trip a token the harness makes through this check.

Why OpenBao verifies and not this code: the services are standard library
only, and Python's standard library has no Ed25519. Asking the vault keeps the
key in one place, with a pass that may only verify. The cost: every action
waits for one call to the vault (1.2 ms at the median, measured in dev mode),
and a vault that cannot be reached refuses every action. Failing closed is the
point: an unverifiable action does not run.

One-time ids live in this process's memory, so a restart forgets them; a token
then still expires within MAX_TTL_S. That includes the restart action itself: the
token that restarted a service is accepted once more by the fresh process, and
could restart it again until it expires (60 s), if someone who captured it sent it
again. A shared store of used ids that survives a restart is not built.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import urllib.request

HEADER = "X-Action-Token"
MAX_TTL_S = 120  # a token valid for longer than this is refused, whatever its signature says
KEY = "actions"
# The service's own path for each action, and the action's name in the credential.
ACTION_NAMES = {"/actions/backoff": "backoff", "/actions/rate-cap": "rate_cap", "/actions/restart": "restart"}

JSON = dict[str, Any]
Verify = Callable[[bytes, str], bool]  # (signed bytes, signature) -> genuine?


def decode(header: str) -> tuple[bytes, str, JSON]:
    """The signed bytes, the signature and the claims of a token. Raises ValueError when malformed. Pure."""
    head, sep, signature = header.partition(".")
    if not sep or not head or not signature:
        raise ValueError("not two parts")
    try:
        signed = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        claims = json.loads(signed)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"unreadable payload: {exc}") from None
    if not isinstance(claims, dict):
        raise ValueError("payload is not a JSON object")
    return signed, signature, claims


def action_args(service: str, path: str, body: Mapping[str, Any]) -> tuple[str, JSON] | None:
    """The action a request asks for, as a credential must name it; None for a path that is not an action. Pure.

    A restart's argument is the service itself, which the request path names;
    the other actions' arguments are their request bodies, exactly.
    """
    name = ACTION_NAMES.get(path)
    if name is None:
        return None
    return name, ({"service": service} if name == "restart" else dict(body))


def _normal(value: Any) -> Any:
    """`value` with every whole float as an int, recursively, so 10 and 10.0 compare equal. Pure."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normal(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    """One spelling for equal JSON values, whatever their key order or number spelling. Pure."""
    return json.dumps(_normal(value), sort_keys=True, separators=(",", ":"))


class Guard:
    """Checks one credential per action: genuine, this action, in time, used once. Thread-safe."""

    def __init__(self, verify: Verify, clock: Callable[[], float] = time.time) -> None:
        """`verify` asks the vault whether a signature over the signed bytes is genuine."""
        self._verify, self.clock = verify, clock
        self._used: dict[str, float] = {}  # one-time id -> expiry
        self._lock = threading.Lock()

    def check(self, header: str | None, action: str, args: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """(the credential's one-time id, None) when it permits this action now; (None, the reason) when not."""
        if not header:
            return None, "no credential"
        try:
            signed, signature, claims = decode(header)
        except ValueError as exc:
            return None, f"malformed credential: {exc}"
        try:
            genuine = self._verify(signed, signature)
        except Exception as exc:  # the vault cannot be reached: fail closed
            return None, f"credential could not be verified: {type(exc).__name__}: {exc}"
        if not genuine:
            return None, "credential signature is not genuine"
        if claims.get("action") != action or _canonical(claims.get("args")) != _canonical(dict(args)):
            return None, f"credential is for {claims.get('action')} {claims.get('args')}, not {action} {dict(args)}"
        exp, jti, now = claims.get("exp"), claims.get("jti"), self.clock()
        if not isinstance(exp, int | float) or isinstance(exp, bool) or exp <= now:
            return None, "credential has expired"
        if exp > now + MAX_TTL_S:
            return None, f"credential is valid for longer than {MAX_TTL_S} s"
        if not isinstance(jti, str) or not jti:
            return None, "credential has no one-time id"
        with self._lock:
            self._used = {k: v for k, v in self._used.items() if v > now}  # forget ids that can no longer be replayed
            if jti in self._used:
                return None, "credential was already used"
            self._used[jti] = float(exp)
        return jti, None


def vault_verify(addr: str, token_file: Path, key: str = KEY) -> Verify:
    """A `Verify` that asks OpenBao's Transit engine, with the verify-only pass read from `token_file` each time.

    Read each time, so a pass rewritten by the vault set-up is picked up with no
    restart. An empty or missing file fails every check.
    """
    url = f"{addr.rstrip('/')}/v1/transit/verify/{key}"

    def verify(signed: bytes, signature: str) -> bool:
        pass_ = token_file.read_text().strip()
        if not pass_:
            raise RuntimeError("no verify pass yet")
        body = json.dumps({"input": base64.b64encode(signed).decode(), "signature": signature}).encode()
        req = urllib.request.Request(url, body, {"X-Vault-Token": pass_, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return bool(json.load(r).get("data", {}).get("valid"))

    return verify


def guard_from_env() -> Guard | None:
    """The guard the compose file configures (BAO_ADDR and BAO_TOKEN_FILE), or None when they are unset.

    None only in unit tests that build a service directly; the compose file sets
    both for every service with actions, and a test checks that it does.
    """
    addr, token_file = os.environ.get("BAO_ADDR"), os.environ.get("BAO_TOKEN_FILE")
    if not addr or not token_file:
        return None
    return Guard(vault_verify(addr, Path(token_file)))
