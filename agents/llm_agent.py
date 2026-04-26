"""Text-only port of Agent-S `gui_agents/s3/core/mllm.py::LMMAgent`.

What we kept from LMMAgent:
    * Mutable `messages` list that the caller appends to before calling
      `get_response()`. Useful for multi-turn reflection loops without
      re-sending the whole history each time.
    * `add_system_prompt`, `add_message(role, content)`, `reset()` API
      surface (plus a `replace_system_prompt` for swapping personas mid-run).

What we dropped from LMMAgent (deliberate):
    * Image/screenshot handling. Aetheer is text-only; v1 of the cognitive
      layer never needs to send an image to a model.
    * The OpenAI SDK dependency. We sit on top of `OpenRouterClient`
      directly, which is async and gives us per-call usage/cost breakdowns
      that LMMAgent's wrapper hid.
    * The implicit "first message is the system prompt" convention. We make
      it explicit so a caller can build a fresh agent and assert that
      `messages[0]["role"] == "system"`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agents.openrouter_client import ChatResult, OpenRouterClient


@dataclass(slots=True)
class LLMAgent:
    """Stateful text-only conversation wrapper around `OpenRouterClient`."""

    client: OpenRouterClient
    name: str = "llm_agent"
    messages: list[dict[str, Any]] = field(default_factory=list)

    # -------- prompt construction helpers --------
    def add_system_prompt(self, content: str) -> None:
        if not content or not content.strip():
            raise ValueError("system prompt cannot be empty")
        if self.messages and self.messages[0]["role"] == "system":
            raise RuntimeError(
                "system prompt already set; use replace_system_prompt() to swap"
            )
        self.messages.insert(0, {"role": "system", "content": content})

    def replace_system_prompt(self, content: str) -> None:
        if not content or not content.strip():
            raise ValueError("system prompt cannot be empty")
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = {"role": "system", "content": content}
        else:
            self.add_system_prompt(content)

    def add_message(self, role: str, content: str | dict | list) -> None:
        if role not in ("user", "assistant", "tool", "system"):
            raise ValueError(f"unsupported role: {role}")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        self.messages.append({"role": role, "content": content})

    def reset(self, *, keep_system: bool = True) -> None:
        if keep_system and self.messages and self.messages[0]["role"] == "system":
            self.messages = [self.messages[0]]
        else:
            self.messages = []

    # -------- inference --------
    async def get_response(
        self,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        append_assistant: bool = True,
    ) -> ChatResult:
        """Send the current `messages` to OpenRouter; return the `ChatResult`.

        If `append_assistant=True` (default), the response content is added
        to `messages` so subsequent calls extend the same trajectory — that's
        the LMMAgent contract that the reflection loop relies on.
        """
        if not self.messages:
            raise RuntimeError("messages empty — call add_system_prompt + add_message first")
        result = await self.client.chat_completion(
            model=model,
            messages=list(self.messages),  # defensive copy
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            tools=tools,
        )
        if append_assistant and result.content:
            self.messages.append({"role": "assistant", "content": result.content})
        return result


__all__ = ["LLMAgent"]
