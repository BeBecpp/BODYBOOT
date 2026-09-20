"""Agent stage: AI decides semantics, never code."""

from __future__ import annotations

from bodyboot.agents.base import (
    AgentBackend,
    AgentError,
    AgentResult,
    AgentUnavailableError,
    InvalidAgentOutputError,
)

AGENT_NAMES = ("claude", "deterministic")


def get_backend(name: str) -> AgentBackend:
    """Instantiate a backend by CLI name."""
    if name == "claude":
        from bodyboot.agents.claude_backend import ClaudeAgentBackend

        return ClaudeAgentBackend()
    if name == "deterministic":
        from bodyboot.agents.deterministic_backend import DeterministicAgentBackend

        return DeterministicAgentBackend()
    raise AgentError(f"unknown agent backend {name!r}; choose one of {', '.join(AGENT_NAMES)}")


__all__ = [
    "AGENT_NAMES",
    "AgentBackend",
    "AgentError",
    "AgentResult",
    "AgentUnavailableError",
    "InvalidAgentOutputError",
    "get_backend",
]
