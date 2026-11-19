"""The real model call: one place that talks to Ollama for the answering model.

Why a module of its own: the call and its settings are infrastructure, the triage
record and its rules are domain. `findyfixy.triage.record` knows nothing about Ollama, so it
can be tested and reused without it. Everything that builds an answering model
(the router, the stress variants, the investigator) takes its settings from here,
so a change to temperature or timeout is made once.

Nothing runs at import time except importing the client library.
"""

from typing import Any

from langchain_ollama import ChatOllama

from findyfixy.triage.record import Decision, Reply, Triage

# How the answering model is called, defined once for every place that builds it
# (`ollama_ask`, the stress variant, the investigator).
#
# Temperature 0 is greedy decoding: the most likely token every time. The same
# alert then gets the same triage, which an on-call tool needs for trust and
# audit, and which makes a quality gate reproducible. Without it Ollama samples
# at its own default, and the same eval's severity score moved from run to run.
# Floating-point differences between runs or machines can still change a token,
# so this is "repeatable in practice", not a guarantee.
#
# Thinking is off. Some models (Qwen3 8B) reason at length before they answer,
# which took more than twice as long on the same alert, and a triage
# tool should not make an on-call engineer wait for it. Models without a
# thinking mode accept the setting and ignore it. Revisit it as an experiment
# if accuracy matters more than latency.
#
# The timeout stops a hung model server from hanging the graph. It applies to
# each read from the server, not to the whole call, so a long answer that keeps
# streaming is not cut off, but a stalled one is. 120 s covers a cold load of
# an 8B model. A timeout raises, and the run fails loudly.
ANSWER_TEMPERATURE = 0
ANSWER_TIMEOUT_S = 120.0


def answer_options() -> dict[str, Any]:
    """Keyword arguments for the answering `ChatOllama`. A fresh dict each call."""
    return {
        "temperature": ANSWER_TEMPERATURE,
        "reasoning": False,
        "client_kwargs": {"timeout": ANSWER_TIMEOUT_S},
    }


def warm_model(model: str) -> None:
    """Load `model` into Ollama's memory, so the next timed call does not pay for it.

    Why: Ollama loads weights on a model's first request. With the agent's
    wall-clock deadline (Day 7), that load counted against the first case's
    budget, so case 1 could be handed to a person for a reason that has nothing
    to do with the model's answer. In production the model is already loaded;
    only a fresh eval process starts cold.

    One request, capped at one generated token, with the same options as every
    answering call. Ollama keeps the model loaded for its keep-alive window
    (5 minutes by default), which every gap between cases is far inside.

    Fails like `ollama_ask`: a stopped server, a model that is not pulled, or a
    stall past the timeout raises, and the run stops before any case starts.
    """
    ChatOllama(model=model, num_predict=1, **answer_options()).invoke("ok")


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
    return _ask(model, prompt, Triage)


def ollama_decision_ask(model: str, prompt: str) -> str:
    """The agent's decision call (Day 7): the same as `ollama_ask`, constrained to `Decision`.

    A separate `ask`, not a flag on `ollama_ask`, so the fixed path's call
    stays byte for byte what it was, and the graph keeps one seam shape.
    """
    return _ask(model, prompt, Decision)


def _ask(model: str, prompt: str, record: type[Triage]) -> Reply:
    """One greedy call, its reply constrained to `record`'s JSON Schema."""
    llm = ChatOllama(model=model, format=record.model_json_schema(), **answer_options())
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
