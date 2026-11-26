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
from wyld3rness.loop.budget import Budget
from wyld3rness.loop.env import Action
from wyld3rness.loop.run import MAX_ACTIONS, Try

Ask = Callable[[str, str, int], str]  # (model, prompt, output cap) -> reply text, possibly a `Reply` with tokens
# The reply's cap: about twice the longest decider reply measured (research log, entry 014).
# It is what lets the budget price a call before it runs; a reply cut at the cap fails validation.
MAX_OUTPUT_TOKENS = 400
# The largest prompt a call can carry, for checking the budget before a run: a first prompt
# (about 2 KB measured in entry 014) plus a retry's echoed invalid reply (_MAX_ECHO_CHARS) and its error.
PROMPT_BYTES_BOUND = 5000
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

Customers are failing now. Choose one action that ends the failures. Add a second action only if one alone \
cannot end them.

Actions:
- {{"name": "backoff", "enabled": true or false}}: gateway retries back off with jitter, within a retry budget.
- {{"name": "rate_cap", "key": "acct-NNNN", "rps": number}}: the gateway refuses one account key's \
requests above this many a second.
- {{"name": "restart", "service": "gateway", "ledger" or "notifications"}}: restart one service. It \
cannot be undone: if it does not work, there are no more tries.

{rules}

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
    """The earlier tries, one line each, for the prompt: what was undone and why, or what the policy refused. Pure."""
    if not history:
        return "Tried so far: nothing."
    lines = []
    for t in history:
        actions = ", ".join(a.label() for a in t.actions)
        if t.verdict == "denied":
            lines.append(
                f"- Try {t.number}: {actions}: refused by the rules, nothing applied ({'; '.join(t.reasons)})."
            )
        else:
            lines.append(f"- Try {t.number}: {actions}: {t.verdict}, undone.")
    return "Tried so far:\n" + "\n".join(lines)


def format_rules(notes: Sequence[str]) -> str:
    """The rules the code enforces, from the policy table, for the prompt. Pure."""
    return "Rules the code enforces:\n" + "\n".join(f"- {n}" for n in notes) if notes else ""


def build_prompt(observation: str, history: Sequence[Try], wait_s: float, notes: Sequence[str] = ()) -> str:
    """The full prompt for one decision. Pure."""
    return PROMPT.format(
        wait_s=wait_s, observation=observation, history=format_history(history), rules=format_rules(notes)
    )


@dataclass(frozen=True)
class Call:
    """One model call: which model, its tokens (None when the server did not say), and whether it was valid."""

    model: str
    served_by: str | None
    input_tokens: int | None
    output_tokens: int | None
    valid: bool


class ModelDecider:
    """The `Decider` behind a model. Every call reserves its worst case on the budget first, and settles after."""

    def __init__(
        self,
        ask: Ask,
        model: str,
        wait_s: float,
        budget: Budget,
        *,
        notes: Sequence[str] = (),
        max_retries: int = MAX_RETRIES,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        """`ask` calls the model (`findyfixy.llm.ollama_reply` bound to `Decision`); `notes` are the policy's rules."""
        self.ask, self.model, self.wait_s, self.budget = ask, model, wait_s, budget
        self.notes, self.max_retries, self.max_output_tokens = tuple(notes), max_retries, max_output_tokens
        self.calls: list[Call] = []
        self.reasons: list[str] = []

    def decide(self, observation: str, history: Sequence[Try]) -> list[Action]:
        """Ask, validate, retry with the error up to `max_retries`.

        Raises `DecisionError` when every attempt is invalid, and `BudgetExceeded`
        when a call does not fit the budget, before that call runs.
        """
        base = build_prompt(observation, history, self.wait_s, self.notes)
        prompt, attempts = base, 0
        while True:
            attempts += 1
            reply = self._call(prompt)
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

    def _call(self, prompt: str) -> str:
        """One model call inside its reservation. A call that raises is charged its reservation: it may have run."""
        reservation = self.budget.reserve(self.model, prompt, self.max_output_tokens)
        try:
            reply = self.ask(self.model, prompt, self.max_output_tokens)
        except Exception:
            self.budget.settle(reservation, None, None)
            raise
        self.budget.settle(reservation, getattr(reply, "input_tokens", None), getattr(reply, "output_tokens", None))
        return reply

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
