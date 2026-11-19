"""The credential broker: a short-lived token per tool call, scoped to one agent, one tool and one resource.

What it does
    `issue(agent, tool, resource)` signs a token that says: this agent may
    read this resource through this tool, for the next five minutes.
    `verify(token, agent=, tool=, resource=)` is what the tool side runs
    before it reads anything: it refuses a token signed with another key,
    issued to another agent, for another tool or resource, or past its expiry.
    The broker issues only what an agent was granted (`grants`), so an agent
    cannot obtain a token for a tool it was never given.

Why per call, and why scoped this narrowly
    A long-lived credential held by an agent is a credential a prompt
    injection can use for anything it covers, for as long as it lives. A token
    minted for one call, one tool and one resource, and dead in five minutes,
    limits what a fooled agent can reach to what it was about to do anyway.
    Naming the agent in the token means that on a team of agents (Day 8) a
    specialist's token cannot be replayed by another agent.

The format
    A JWT (RFC 7519) signed with HMAC-SHA256, through PyJWT: the standard
    format, so a tool behind another process or service can verify it with
    any JWT library. Claims: `iss` (the broker), `sub` (the agent), `aud`
    (the tool), `scope` ("read:" plus the resource), `iat`, `exp` and `jti` (a
    unique id per token, so a log can tell two calls apart).

The key
    Random per broker, held in memory, unless one is passed in. Today the
    tools run in the agent's process, so a token never leaves it and a stored
    secret would add risk without benefit. When a tool moves behind a process
    or network boundary, both sides need the key: it then comes from
    configuration (a secret manager in production), and nothing else here
    changes. With an asymmetric key (RS256 or EdDSA) a verifier could check
    tokens without being able to mint them; that is the change to make once
    verifiers are separate services.

What this does not do
    In one process, issuing and verifying trust the same memory: the tests
    prove the contract (refusals for the wrong agent, tool, resource, key or
    time), not isolation between processes. Revocation before expiry is not
    supported; the five-minute life is the bound.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import secrets
import time
from typing import Any

import jwt

ISSUER = "wyld3rness"
ALGORITHM = "HS256"
TTL_S = 300  # five minutes: one call, plus slack for a slow tool, never a session


class TokenRefused(Exception):
    """A token was not issued, or did not verify. The message says which check failed, in words a person can act on."""


@dataclass(frozen=True)
class Claims:
    """What a verified token grants."""

    agent: str
    tool: str
    resource: str
    issued_at: int
    expires_at: int
    token_id: str


class CredentialBroker:
    """Issues and verifies per-call tokens. Nothing is signed until `issue`.

    `grants` maps each agent to the tools it may call. `clock` returns Unix
    seconds; tests pass a fake one to step past the expiry.
    """

    def __init__(
        self,
        grants: Mapping[str, frozenset[str]],
        *,
        key: bytes | None = None,
        ttl_s: int = TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the grants, the key (random when not given) and the lifetime."""
        if ttl_s <= 0:
            raise ValueError("a token must expire: ttl_s must be positive")
        self._grants = {agent: frozenset(tools) for agent, tools in grants.items()}
        self._key = key if key is not None else secrets.token_bytes(32)
        self._ttl = ttl_s
        self._clock = clock

    def granted(self, agent: str, tool: str) -> bool:
        """Whether `agent` may call `tool`: what a caller checks once, when it wires the tool."""
        return tool in self._grants.get(agent, frozenset())

    def issue(self, agent: str, tool: str, resource: str) -> str:
        """A token for one call. Raises `TokenRefused` when the agent was not granted the tool."""
        if not self.granted(agent, tool):
            raise TokenRefused(f"{agent} is not granted {tool}")
        now = int(self._clock())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": agent,
            "aud": tool,
            "scope": f"read:{resource}",
            "iat": now,
            "exp": now + self._ttl,
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(claims, self._key, algorithm=ALGORITHM)

    def verify(self, token: str, *, agent: str, tool: str, resource: str) -> Claims:
        """Check a token before a tool reads `resource` for `agent`. Raises `TokenRefused` with the failed check.

        PyJWT checks the signature, the issuer, the audience and that every
        claim is present. Time is checked here, on the broker's own clock, so
        both checks are testable and have no leeway: a token issued in the
        future is refused, and so is one at or past its expiry.
        """
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=tool,
                issuer=ISSUER,
                options={
                    "require": ["iss", "sub", "aud", "scope", "iat", "exp", "jti"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidAudienceError as err:
            raise TokenRefused(f"token is not for {tool}") from err
        except jwt.InvalidSignatureError as err:
            raise TokenRefused("token was not signed by this broker") from err
        except jwt.InvalidTokenError as err:
            raise TokenRefused(f"token is invalid: {err}") from err
        if claims["sub"] != agent:
            raise TokenRefused(f"token was issued to {claims['sub']}, not {agent}")
        if claims["scope"] != f"read:{resource}":
            raise TokenRefused(f"token covers {claims['scope']}, not read:{resource}")
        now = int(self._clock())
        if claims["iat"] > now:
            raise TokenRefused("token is issued in the future")
        if now >= claims["exp"]:
            raise TokenRefused("token has expired")
        return Claims(agent, tool, resource, claims["iat"], claims["exp"], claims["jti"])
