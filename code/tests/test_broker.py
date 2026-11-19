"""The credential broker (harness): a token for one agent, one tool, one resource, for five minutes, and nothing else.

A fake clock, no app, no network.
"""

from __future__ import annotations

import jwt
import pytest

from wyld3rness.identity.broker import ALGORITHM, CredentialBroker, TokenRefused

GRANTS = {"changes": frozenset({"get_recent_changes"}), "memory": frozenset({"search_incidents"})}


class Clock:
    """Unix seconds, moved by hand."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def broker(clock: Clock) -> CredentialBroker:
    return CredentialBroker(GRANTS, clock=clock)


def test_a_granted_call_gets_a_token_that_verifies_for_exactly_that_call(broker: CredentialBroker) -> None:
    token = broker.issue("changes", "get_recent_changes", "changes:auth")
    claims = broker.verify(token, agent="changes", tool="get_recent_changes", resource="changes:auth")
    assert (claims.agent, claims.tool, claims.resource) == ("changes", "get_recent_changes", "changes:auth")
    assert claims.expires_at - claims.issued_at == 300


def test_an_agent_cannot_get_a_token_for_a_tool_it_was_not_granted(broker: CredentialBroker) -> None:
    with pytest.raises(TokenRefused, match="memory is not granted get_recent_changes"):
        broker.issue("memory", "get_recent_changes", "changes:auth")
    with pytest.raises(TokenRefused, match="stranger is not granted"):
        broker.issue("stranger", "search_incidents", "incidents")


@pytest.mark.parametrize(
    ("agent", "tool", "resource", "message"),
    [
        ("memory", "get_recent_changes", "changes:auth", "issued to changes, not memory"),
        ("changes", "search_incidents", "changes:auth", "not for search_incidents"),
        ("changes", "get_recent_changes", "changes:billing", "covers read:changes:auth, not read:changes:billing"),
    ],
)
def test_a_token_is_refused_for_another_agent_tool_or_resource(
    broker: CredentialBroker, agent: str, tool: str, resource: str, message: str
) -> None:
    token = broker.issue("changes", "get_recent_changes", "changes:auth")
    with pytest.raises(TokenRefused, match=message):
        broker.verify(token, agent=agent, tool=tool, resource=resource)


def test_a_token_dies_at_five_minutes_with_no_leeway(broker: CredentialBroker, clock: Clock) -> None:
    token = broker.issue("changes", "get_recent_changes", "changes:auth")
    clock.now += 299
    broker.verify(token, agent="changes", tool="get_recent_changes", resource="changes:auth")
    clock.now += 1
    with pytest.raises(TokenRefused, match="expired"):
        broker.verify(token, agent="changes", tool="get_recent_changes", resource="changes:auth")


def test_a_token_from_another_broker_or_altered_is_refused(broker: CredentialBroker, clock: Clock) -> None:
    other = CredentialBroker(GRANTS, clock=clock)  # its own random key
    forged = other.issue("changes", "get_recent_changes", "changes:auth")
    with pytest.raises(TokenRefused, match="not signed by this broker"):
        broker.verify(forged, agent="changes", tool="get_recent_changes", resource="changes:auth")
    unsigned = jwt.encode({"sub": "changes"}, "", algorithm="none")
    with pytest.raises(TokenRefused, match="invalid"):
        broker.verify(unsigned, agent="changes", tool="get_recent_changes", resource="changes:auth")


def test_a_token_missing_a_claim_is_refused(clock: Clock) -> None:
    """Signed with the right key but without `scope`: every claim is required."""
    key = b"k" * 32
    broker = CredentialBroker(GRANTS, key=key, clock=clock)
    partial = jwt.encode(
        {"iss": "wyld3rness", "sub": "changes", "aud": "get_recent_changes", "iat": 1, "exp": 2, "jti": "x"},
        key,
        algorithm=ALGORITHM,
    )
    with pytest.raises(TokenRefused, match="invalid"):
        broker.verify(partial, agent="changes", tool="get_recent_changes", resource="changes:auth")


def test_every_token_is_distinct_and_a_token_must_expire(broker: CredentialBroker) -> None:
    tokens = {broker.issue("memory", "search_incidents", "incidents") for _ in range(20)}
    assert len(tokens) == 20
    with pytest.raises(ValueError, match="must expire"):
        CredentialBroker(GRANTS, ttl_s=0)


def test_a_token_issued_in_the_future_is_refused(broker: CredentialBroker, clock: Clock) -> None:
    token = broker.issue("changes", "get_recent_changes", "changes:auth")
    clock.now -= 10  # the verifier's clock is behind the issuer's
    with pytest.raises(TokenRefused, match="future"):
        broker.verify(token, agent="changes", tool="get_recent_changes", resource="changes:auth")
