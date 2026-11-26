"""The vault's set-up, from the host: OpenBao's signing key, and two passes that can each do one thing.

    make sim-up
      1. simprod-vault init   .bao/ (0700) with empty pass files, so compose can mount them
      2. docker compose up    OpenBao starts as a server with in-memory storage: empty and sealed
      3. simprod-vault setup  initialise it if it is empty and unseal it if sealed, through the API,
                              keeping the root token and the unseal key in .bao/ (0600); then the
                              Transit engine, the Ed25519 key `actions` (never exportable), a policy
                              that may only sign with it and one that may only verify, and a pass for
                              each: .bao/sign.token (host only, 0600) and .bao/verify.token (mounted
                              read-only into the services)

Nothing here prints a key, a token or a pass. `.bao/` is git-ignored and kept
out of every build context (.dockerignore).

Why a server with in-memory storage and not dev mode: dev mode prints its root
token and unseal key to the container log, where anyone running
`docker compose logs` sees them. Initialising through the API returns both to
this process only. In-memory storage starts empty at every OpenBao restart, so
a restart needs `make sim-up` again; the services read the verify pass at every
check, so new passes need no restart of theirs. Production keeps storage that
persists and an unseal procedure of its own.

`check` runs before every experiment (the controls, `make oncall`): it signs a
probe, so an expired pass or a sealed vault stops the run before any storm, and
it compares this host's clock with the VM's, because tokens are signed here and
checked there and a skew past their lifetime refuses every action.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from simprod.credentials import ACTION_NAMES
from wyld3rness.loop.credential import KEY, TTL_S, TransitSigner
from wyld3rness.loop.env import Action

DIR = Path(__file__).resolve().parents[2] / ".bao"  # code/examples/simprod/.bao
ROOT, UNSEAL = DIR / "root.token", DIR / "unseal.key"
SIGN, VERIFY = DIR / "sign.token", DIR / "verify.token"
HOST_ADDR = "http://127.0.0.1:8200"
PASS_TTL = "720h"  # 30 days: longer than any sim stays up; an OpenBao restart replaces them anyway
MAX_SKEW_S = TTL_S / 4  # a quarter of a token's life
POLICIES = {
    "sign": f'path "transit/sign/{KEY}" {{ capabilities = ["update"] }}',
    "verify": f'path "transit/verify/{KEY}" {{ capabilities = ["update"] }}',
}


def _write_secret(path: Path, value: str, mode: int = 0o600) -> None:
    """Write `value` to `path` with `mode`, never echoing it."""
    path.write_text(value)
    path.chmod(mode)


def init() -> None:
    """Create `.bao/` and empty pass files, so compose can mount the verify pass before it is filled."""
    DIR.mkdir(mode=0o700, exist_ok=True)
    for path, mode in ((SIGN, 0o600), (VERIFY, 0o644)):
        path.touch(exist_ok=True)
        path.chmod(mode)


def _call(method: str, path: str, body: dict[str, object] | None, token: str = "") -> dict[str, object]:
    """One call to OpenBao's HTTP API on the host. Raises with OpenBao's error text on failure."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Vault-Token": token} if token else {}
    req = urllib.request.Request(f"{HOST_ADDR}/v1/{path}", data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}") from None
    answer: dict[str, object] = json.loads(raw) if raw else {}
    return answer


def _health() -> dict[str, object]:
    """OpenBao's health, whatever its state (it answers non-200 codes when uninitialised or sealed)."""
    return _call("GET", "sys/health?uninitcode=200&sealedcode=200&standbycode=200", None)


def _unsealed_root() -> str:
    """Initialise OpenBao if it is empty and unseal it if sealed; return the root token."""
    if not _health().get("initialized"):
        answer = _call("PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1})
        keys, root = answer.get("keys_base64"), answer.get("root_token")
        if not isinstance(keys, list) or not keys or not isinstance(root, str):
            raise RuntimeError("OpenBao's init answer had no key or root token")
        _write_secret(UNSEAL, str(keys[0]))
        _write_secret(ROOT, root)
    if _health().get("sealed"):
        _call("PUT", "sys/unseal", {"key": UNSEAL.read_text().strip()})
    return ROOT.read_text().strip()


def setup() -> None:
    """Initialise and unseal if needed; enable Transit, create the key, the two policies and a pass for each."""
    root = _unsealed_root()
    mounts = _call("GET", "sys/mounts", None, root)
    listed = mounts.get("data", mounts)
    if not isinstance(listed, dict) or "transit/" not in listed:
        _call("POST", "sys/mounts/transit", {"type": "transit"}, root)
    _call("POST", f"transit/keys/{KEY}", {"type": "ed25519", "exportable": False}, root)
    for name, rules in POLICIES.items():
        _call("PUT", f"sys/policies/acl/{name}", {"policy": rules}, root)
    for name, path in (("sign", SIGN), ("verify", VERIFY)):
        body = {"policies": [name], "no_default_policy": True, "ttl": PASS_TTL}
        auth = _call("POST", "auth/token/create", body, root).get("auth")
        if not isinstance(auth, dict) or not auth.get("client_token"):
            raise RuntimeError(f"no {name} pass in OpenBao's answer")
        _write_secret(path, str(auth["client_token"]), 0o600 if name == "sign" else 0o644)


def clock_skew_s() -> float:
    """This host's clock minus the VM's, from OpenBao's own report of its time."""
    reported = _health().get("server_time_utc")
    if not isinstance(reported, int | float):
        raise RuntimeError("OpenBao did not report its time")
    return time.time() - float(reported)


def check() -> None:
    """Fail loudly before an experiment if signing cannot work: no pass, a sealed vault, or a skewed clock."""
    skew = clock_skew_s()
    if abs(skew) > MAX_SKEW_S:
        raise RuntimeError(
            f"the host's and the VM's clocks differ by {skew:.0f} s, past {MAX_SKEW_S:.0f} s: tokens would be refused"
        )
    signer().sign(Action("check", {"at": datetime.now(UTC).isoformat(timespec="seconds")}))


def signer() -> TransitSigner:
    """The host's signer: OpenBao on 127.0.0.1:8200, with the sign-only pass."""
    pass_ = SIGN.read_text().strip()
    if not pass_:
        raise RuntimeError("no sign pass: run `make sim-up`")
    return TransitSigner(HOST_ADDR, pass_)


def action_of(path: str, body: dict[str, object]) -> Action:
    """The action an edge request asks for, as its credential must name it. Pure.

    `/gateway/actions/rate-cap` with {key, rps} is rate_cap(key, rps);
    `/ledger/actions/restart` is restart(service=ledger).
    """
    service, _, local = path.lstrip("/").partition("/")
    name = ACTION_NAMES.get(f"/{local}")
    if name is None:
        raise ValueError(f"{path} is not an action")
    return Action(name, {"service": service} if name == "restart" else dict(body))


def main(argv: list[str] | None = None) -> int:
    """`init`, `setup` or `check`."""
    parser = argparse.ArgumentParser(prog="simprod-vault", description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=["init", "setup", "check"])
    args = parser.parse_args(argv)
    if args.cmd == "init":
        init()
        print(f"vault folder ready: {DIR.name}/")
    elif args.cmd == "setup":
        setup()
        check()
        print(f"vault ready: key {KEY!r}, a sign pass and a verify pass in {DIR.name}/ (none shown)")
    else:
        check()
        print(f"vault check passed: signing works, clock skew {clock_skew_s():+.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
