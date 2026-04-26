"""Prompt loading layer for v3 agents (reads docs/AGENT_PROTOCOL.json)."""
from agents.prompts.loader import (
    AgentPromptSpec,
    PromptLoader,
    default_loader,
)

__all__ = ["AgentPromptSpec", "PromptLoader", "default_loader"]
