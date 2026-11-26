"""The real model call: one place that talks to Ollama for the answering model.

Why a module of its own: the call and its settings are infrastructure, the triage
record and its rules are domain. `findyfixy.triage.record` knows nothing about Ollama, so it
can be tested and reused without it. Everything that builds an answering model
(the router, the stress variants, the investigator, the on-call decider) takes its settings from here,
so a change to temperature or timeout is made once.

Nothing runs at import time except importing the client library.
"""

from typing import Any

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from findyfixy.triage.record import Reply, Triage

# How the answering model is called, defined once for every place that builds it
# (`ollama_ask`, the stress variant, the investigator).
#
# Temperature 0 is greedy decoding: the most likely token every time. The same
# alert then gets the same triage, which an on-call tool needs for trust and
# audit, and which makes a quality gate reproducible. Without it Ollama samples
# at its own default, and the same eval scored 9, 11 and 16 of 30 on severity.
# Floating-point differences between runs or machines can still change a token,
# so this is "repeatable in practice", not a guarantee.
#
# Thinking is off. Some models (Qwen3 8B) reason at length before they answer,
# which took 17.6 s against 6.9 s with it off on the same alert, and a triage
# tool should not make an on-call engineer wait for it. Models without a
# thinking mode accept the setting and ignore it. Revisit it as an experiment
# if accuracy matters more than latency.
#
# The timeout stops a hung model server from hanging the graph. It applies to
# each read from the server, not to the whole call, so a long answer that keeps
# streaming is not cut off, but a stalled one is. 120 s covers a cold load of
# the 7B model. A timeout raises, and the run fails loudly.
ANSWER_TEMPERATURE = 0
ANSWER_TIMEOUT_S = 120.0


def answer_options() -> dict[str, Any]:
    """Keyword arguments for the answering `ChatOllama`. A fresh dict each call."""
    return {
        "temperature": ANSWER_TEMPERATURE,
        "reasoning": False,
        "client_kwargs": {"timeout": ANSWER_TIMEOUT_S},
    }


def ollama_ask(model: str, prompt: str) -> str:
    """The real `ask`: call one Ollama model and return its raw reply text.

    `format=<JSON Schema>` turns on constrained decoding: Ollama masks tokens
    that would break the schema, so the reply is JSON of the right shape.
    Values are checked later, in `ask_validated`. We return text, not a parsed
    object, so a bad value fails in our code with a readable message.

    We build the client on every call. That is cheap because it is only a
    thin HTTP client. The expensive part is Ollama loading model weights into
    memory. On a 16 GB Mac, switching from the 1.5B to the 7B model can add a
    load delay, so an escalated run pays for a model swap on top of two calls.

    The call is greedy (temperature 0) and has a read timeout; see
    `answer_options`.

    Fails when: Ollama is not running (connection error), the model is not
    pulled (404), or the server stalls past the timeout. Nothing here retries.
    """
    return ollama_reply(model, prompt, Triage)


def ollama_reply(model: str, prompt: str, schema: type[BaseModel], max_output_tokens: int | None = None) -> Reply:
    """One greedy, constrained call: a reply shaped as `schema`'s JSON, not yet validated.

    Shared by triage (`ollama_ask`) and the on-call decider
    (`findyfixy.oncall.decide`), so every answering call has the same
    temperature, thinking setting and timeout. Fails as `ollama_ask` does.
    `max_output_tokens` caps the reply (Ollama's `num_predict`), which is what
    lets the budget price a call before it runs; a reply cut at the cap is
    invalid JSON and fails validation. None leaves triage as it was: no cap.
    """
    # None is ChatOllama's own default for num_predict, so triage's calls are unchanged.
    llm = ChatOllama(model=model, format=schema.model_json_schema(), num_predict=max_output_tokens, **answer_options())
    return reply_from(llm.invoke(prompt))


def reply_from(message: Any) -> Reply:
    """The `Reply` for one chat message: its text, the model that served it, and its tokens.

    Ollama names the model that served the request in its response. Keep it
    next to the text, so a log can show what ACTUALLY answered, not only what
    we asked for. The token counts come from the same response
    (`prompt_eval_count` and `eval_count`, which langchain reports as
    `usage_metadata`). Absent metadata gives None (unknown), never a guess.
    """
    usage = message.usage_metadata or {}
    return Reply(
        message.content,
        served_by=message.response_metadata.get("model"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )
