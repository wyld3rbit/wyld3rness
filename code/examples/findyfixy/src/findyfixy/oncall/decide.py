"""The decider: a model reads the metrics and what was tried, and proposes 1 or 2 typed actions.

    observation + earlier tries ──► prompt ──► model (JSON constrained to `Decision`)
        ──► validate ──► valid: the actions, to the harness's loop
                     └─► invalid: one retry with the error ──► still invalid: DecisionError

This is the harness's `Decider` port (`code/src/wyld3rness/loop/run.py`). The
loop, not this module, applies, verifies, undoes and stops; a proposal here
changes nothing until the loop applies it.

Why `reason` comes before `actions` in the schema: a constrained model writes
the fields in order, so it states what it read before it commits to an action.
Why a restart is offered at all: an operator has it, and the loop's rule for an
action without undo (no retry after it) is part of what is being tested. A cap
must be a positive rate here; removing a cap is only ever an undo.

Validation follows the triage pattern (`findyfixy.triage.record.ask_validated`):
connection errors and timeouts are not caught, because they are faults of the
model server, not bad output, and the loop turns them into the safe state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from findyfixy.oncall.env import KEY
from wyld3rness.loop.env import Action
from wyld3rness.loop.run import MAX_ACTIONS, Try

Ask = Callable[[str, str], str]  # (model, prompt) -> reply text, possibly a `Reply` with token counts
MAX_RETRIES = 1
_MAX_ECHO_CHARS = 2000


class ActionChoice(BaseModel):
    """One proposed action. Flat, so a small model's constrained output stays simple; checked per name."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["backoff", "rate_cap", "restart"]
    enabled: bool | None = None
    key: str | None = None
    rps: float | None = None
    service: Literal["gateway", "ledger", "notifications"] | None = None

    @model_validator(mode="after")
    def _fields_match_the_name(self) -> Self:
        """Each action carries exactly its own arguments."""
        given = {f for f in ("enabled", "key", "rps", "service") if getattr(self, f) is not None}
        needed = {"backoff": {"enabled"}, "rate_cap": {"key", "rps"}, "restart": {"service"}}[self.name]
        if given != needed:
            raise ValueError(f"{self.name} takes exactly {sorted(needed)}, got {sorted(given)}")
        if self.key is not None and not KEY.fullmatch(self.key):
            raise ValueError(f"key {self.key!r} is not an account key like acct-0042")
        if self.rps is not None and self.rps <= 0:
            raise ValueError("rps must be a positive number of requests a second")
        return self

    def to_action(self) -> Action:
        """The harness's typed action."""
        args = {f: getattr(self, f) for f in ("enabled", "key", "rps", "service") if getattr(self, f) is not None}
        return Action(self.name, args)


class Decision(BaseModel):
    """What the model reads into the metrics, then the actions to apply together."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=600)
    actions: list[ActionChoice] = Field(min_length=1, max_length=MAX_ACTIONS)


class DecisionError(Exception):
    """Every attempt was invalid. Carries the model and the attempt count; the cause is the last error."""

    def __init__(self, model: str, attempts: int) -> None:
        super().__init__(f"{model}: no valid decision in {attempts} attempts")
        self.model, self.attempts = model, attempts


PROMPT = """You are on call for a payment system. No person approves your actions. Code applies \
them together, waits {wait_s:g} s, checks what customers see, and undoes them if customers have not \
recovered and stayed recovered.

The system: customers pay through a gateway. The gateway writes each payment to a ledger of 4 \
shards (an account key always lands on the same shard), retries a failed write, then notifies.

Customers are failing now. Choose 1 or 2 actions that end the failures.

Actions:
- {{"name": "backoff", "enabled": true or false}}: gateway retries back off with jitter, within a retry budget.
- {{"name": "rate_cap", "key": "acct-NNNN", "rps": number}}: the gateway refuses one account key's \
requests above this many a second.
- {{"name": "restart", "service": "gateway", "ledger" or "notifications"}}: restart one service. It \
cannot be undone: if it does not work, there are no more tries.

Metrics now:
{observation}

{history}

Reply with JSON: {{"reason": "what the metrics show, in one or two sentences", "actions": [...]}}."""

RETRY_PROMPT = """{prompt}

Your previous reply was not valid:
{reply}

The error:
{error}

Reply again with valid JSON only."""


def format_history(history: Sequence[Try]) -> str:
    """The earlier tries, one line each, for the prompt. Pure."""
    if not history:
        return "Tried so far: nothing."
    lines = [f"- Try {t.number}: {', '.join(a.label() for a in t.actions)}: {t.verdict}, undone." for t in history]
    return "Tried so far, each undone because customers did not recover and stay recovered:\n" + "\n".join(lines)


def build_prompt(observation: str, history: Sequence[Try], wait_s: float) -> str:
    """The full prompt for one decision. Pure."""
    return PROMPT.format(wait_s=wait_s, observation=observation, history=format_history(history))


@dataclass(frozen=True)
class Call:
    """One model call: which model, its tokens (None when the server did not say), and whether it was valid."""

    model: str
    served_by: str | None
    input_tokens: int | None
    output_tokens: int | None
    valid: bool


class ModelDecider:
    """The `Decider` behind a model. Records every call, for the scorecard and the budget (step 4)."""

    def __init__(self, ask: Ask, model: str, wait_s: float, max_retries: int = MAX_RETRIES) -> None:
        """`ask` is `findyfixy.llm.ollama_reply` bound to `Decision` in production, a fake in tests."""
        self.ask, self.model, self.wait_s, self.max_retries = ask, model, wait_s, max_retries
        self.calls: list[Call] = []
        self.reasons: list[str] = []

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        """Ask, validate, retry with the error up to `max_retries`. Raises `DecisionError` when all are invalid."""
        base = build_prompt(observation, history, self.wait_s)
        prompt, attempts = base, 0
        while True:
            attempts += 1
            reply = self.ask(self.model, prompt)
            try:
                decision = Decision.model_validate_json(reply)
            except ValidationError as err:
                self._record(reply, valid=False)
                if attempts > self.max_retries:
                    raise DecisionError(self.model, attempts) from err
                prompt = RETRY_PROMPT.format(prompt=base, reply=reply[:_MAX_ECHO_CHARS], error=err)
                continue
            self._record(reply, valid=True)
            self.reasons.append(decision.reason)
            return [c.to_action() for c in decision.actions]

    def _record(self, reply: str, *, valid: bool) -> None:
        """Keep one call's model and tokens, as the reply reports them."""
        self.calls.append(
            Call(
                self.model,
                getattr(reply, "served_by", None),
                getattr(reply, "input_tokens", None),
                getattr(reply, "output_tokens", None),
                valid,
            )
        )
