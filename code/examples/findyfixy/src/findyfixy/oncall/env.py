"""FindyFixy's environment over simprod: the harness's `Environment` port, through the edge only.

    observe()  GET /gateway, /ledger, /customers metrics ──► the samples an operator reads
    apply(a,c) read the gateway's settings (for the undo) ──► POST /<service>/actions/... as X-Actor findyfixy,
               with the credential the loop signed for exactly this action
    series()   the storm driver's scraper, in the host process: what customers saw
    now/wait   the storm's clock, real time

The three typed actions and their undo:

    action                          request through the edge                    undo
    backoff(enabled)                POST /gateway/actions/backoff               backoff as it was before
    rate_cap(key, rps)              POST /gateway/actions/rate-cap              that key's cap as it was before
    restart(service)                POST /<service>/actions/restart             none

Why the undo is the previous value and not the opposite action: turning backoff
on when it was already on is undone by leaving it on. Why a failed settings read
gives no undo: a guessed undo could leave a change nobody chose, and no undo makes
the loop stop retrying instead (`code/src/wyld3rness/loop/run.py`).

Trust: the metrics text is written by services the agent's actions affect, and
its key labels come from request data, so it is untrusted input to the model.
The injection suite (Day 7, step 5) attacks this path. The model never reaches
this module's HTTP calls: it proposes, the loop applies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
import time
import urllib.request

from simprod.controls import EDGE, act, read_settings
from wyld3rness.loop.env import Action, Applied
from wyld3rness.loop.verdict import Second

ACTOR = "findyfixy"
SERVICES = ("gateway", "ledger", "notifications")
# simprod's account keys (loadgen.py). Used with fullmatch: `$` would accept a trailing newline.
KEY = re.compile(r"acct-\d{4}")
# The metrics an operator reads in a payments incident, by service. Comments and the
# Python process metrics are left out: they add tokens and say nothing about the storm.
OBSERVED: Mapping[str, tuple[str, ...]] = {
    "gateway": (
        "gateway_payments_total",
        "gateway_ledger_attempts_total",
        "gateway_retries_total",
        "gateway_retries_denied_total",
        "gateway_backoff_enabled",
        "gateway_rate_caps",
        "gateway_top_keys_rps",
    ),
    "ledger": ("ledger_writes_total", "ledger_queue_depth", "ledger_busy_slots"),
    "customers": ("customers_requests_total", "customers_rate"),
}

JSON = dict[str, object]


def observation(texts: Mapping[str, str]) -> str:
    """The observed samples from each service's Prometheus text, grouped by service. Pure."""
    blocks = []
    for service, names in OBSERVED.items():
        lines = [
            line
            for line in texts.get(service, "").splitlines()
            if not line.startswith("#") and line.split("{", 1)[0].split(" ", 1)[0] in names
        ]
        blocks.append(f"[{service}]\n" + ("\n".join(lines) or "(no samples)"))
    return "\n\n".join(blocks)


def to_request(action: Action) -> tuple[str, JSON]:
    """The edge path and body for one action. Raises ValueError for an unknown action or bad arguments. Pure."""
    args = dict(action.args)
    match action.name:
        case "backoff" if isinstance(args.get("enabled"), bool) and args.keys() == {"enabled"}:
            return "/gateway/actions/backoff", args
        case "rate_cap" if args.keys() == {"key", "rps"} and KEY.fullmatch(str(args["key"])) and _rps(args["rps"]):
            return "/gateway/actions/rate-cap", args
        case "restart" if args.keys() == {"service"} and args["service"] in SERVICES:
            return f"/{args['service']}/actions/restart", {}
    raise ValueError(f"not a valid action: {action.label()}")


def _rps(value: object) -> bool:
    """A cap is a positive number of requests a second, or None to remove one. Pure."""
    return value is None or (isinstance(value, int | float) and not isinstance(value, bool) and value > 0)


def undo_for(action: Action, before: Mapping[str, object]) -> Action | None:
    """The action that puts back what `action` changes, from the gateway's settings before it. Pure.

    None for a restart (no undo) and when the settings could not be read.
    """
    if "error" in before:
        return None
    if action.name == "backoff":
        return Action("backoff", {"enabled": bool(before["backoff"])})
    caps = before.get("caps")
    if action.name == "rate_cap" and isinstance(caps, Mapping):
        return Action("rate_cap", {"key": action.args["key"], "rps": caps.get(action.args["key"])})
    return None


def fetch_text(url: str) -> str:
    """GET one URL through the edge; raises on any failure, which the loop turns into the safe state."""
    with urllib.request.urlopen(url, timeout=5) as r:
        text: str = r.read().decode()
        return text


class SimprodEnv:
    """The `Environment` for one simprod storm. Every HTTP call is injected, so tests run without Docker."""

    def __init__(
        self,
        series: Callable[[], Sequence[Second]],
        now: Callable[[], float],
        *,
        actor: str = ACTOR,
        post: Callable[[str, Mapping[str, object], str, str], JSON] = act,
        get: Callable[[str], str] = fetch_text,
        settings: Callable[[], Mapping[str, object]] = read_settings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`series` and `now` come from the storm driver (`simprod.controls.live_storm`)."""
        self._series, self._now, self.actor = series, now, actor
        self._post, self._get, self._settings, self._sleep = post, get, settings, sleep
        self.sent: list[JSON] = []  # every request sent, actions and undos, as `act` answered it

    def observe(self) -> str:
        """The observed metrics of the gateway, the ledger and the customers, now."""
        return observation({s: self._get(f"{EDGE}/{s}/metrics") for s in OBSERVED})

    def apply(self, action: Action, credential: str) -> Applied:
        """Validate, read the settings for the undo, send with its credential. An invalid action is never sent."""
        try:
            path, body = to_request(action)
        except ValueError as exc:
            return Applied(action, False, f"refused: {exc}", None)
        undo = undo_for(action, self._settings())
        try:
            answer = self._post(path, body, self.actor, credential)
        except Exception as exc:  # sent, then the reply was lost or unreadable: it may be in force
            answer = {"path": path, "body": body, "status": 0, "answer": f"{type(exc).__name__}: {exc}"}
        self.sent.append(answer)
        # Any 2xx is carried out: a restart answers 202 (accepted, then the process exits). Whether
        # it worked is the verdict's to say, not the status code's.
        carried_out = isinstance(answer["status"], int) and 200 <= answer["status"] < 300
        return Applied(action, carried_out, f"{answer['status']} {answer['answer']}", undo)

    def series(self) -> Sequence[Second]:
        """What customers saw, per second, from the storm driver's scraper."""
        return self._series()

    def now(self) -> float:
        """The storm's clock."""
        return self._now()

    def wait(self, seconds: float) -> None:
        """Real time passes."""
        self._sleep(seconds)
