from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from openai import OpenAI
from openai import APIError

from .config import LLMConfig


LOGGER = logging.getLogger(__name__)

_RETRY_INVALID_JSON_MESSAGE = (
    "Предыдущий ответ не был валидным JSON. Ответь строго валидным JSON согласно схеме."
)
_RETRY_TRUNCATED_MESSAGE = (
    "Предыдущий ответ был обрезан из-за ограничений длины. Сократи формулировки, но сохрани структуру и верни валидный JSON."
)
_RETRY_MINIMAL_JSON_MESSAGE = (
    "Верни максимально короткий валидный JSON: лаконичные формулировки, без пояснительных текстов."
)


def _append_system_hints(messages: List[Dict[str, str]], hints: List[str]) -> List[Dict[str, str]]:
    """Return a fresh copy of messages extended with additional system hints."""

    updated = [msg.copy() for msg in messages]
    for hint in hints:
        updated.append({"role": "system", "content": hint})
    return updated


class LLMClient:
    """Wrapper around OpenAI client with JSON enforcement."""

    def __init__(self, conf: LLMConfig) -> None:
        api_key = conf.api_key or os.environ.get(conf.api_key_env)
        if not api_key:
            msg = (
                "OpenAI API key is required. Provide it in the configuration or set the "
                f"{conf.api_key_env} environment variable."
            )
            raise RuntimeError(msg)

        base_url = conf.base_url or os.environ.get(conf.base_url_env)
        model_name = conf.model or os.environ.get(conf.model_env)
        if not model_name:
            msg = (
                "OpenAI model identifier is required. Provide it in the configuration or set the "
                f"{conf.model_env} environment variable."
            )
            raise RuntimeError(msg)

        self._conf = conf
        self._model = model_name
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=conf.organization,
        )

    @property
    def model_name(self) -> str:
        """Return the model identifier configured for this client."""

        return self._model

    def generate(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Call the chat completion endpoint and return parsed JSON content."""

        max_attempts = self._conf.max_retries
        current_messages = [msg.copy() for msg in messages]
        last_content: str | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=current_messages,
                    temperature=self._conf.temperature,
                    max_tokens=self._conf.max_output_tokens,
                    response_format={"type": "json_object"},
                    timeout=self._conf.request_timeout,
                )
            except APIError as exc:  # pragma: no cover - passthrough
                raise RuntimeError(f"LLM request failed: {exc}") from exc

            if not response.choices:
                raise RuntimeError("LLM response does not contain choices")

            choice = response.choices[0]
            content = (choice.message.content or "").strip()
            last_content = content
            finish_reason = getattr(choice, "finish_reason", None)

            if finish_reason and finish_reason != "stop":
                if finish_reason == "length" and attempt < max_attempts:
                    LOGGER.warning(
                        "LLM response truncated (finish_reason=length); retrying with adjusted prompt (%s/%s)",
                        attempt,
                        max_attempts,
                    )
                    hints = [_RETRY_TRUNCATED_MESSAGE]
                    if attempt + 1 == max_attempts:
                        hints.append(_RETRY_MINIMAL_JSON_MESSAGE)
                    current_messages = _append_system_hints(messages, hints)
                    continue

                raise RuntimeError(
                    f"LLM response ended prematurely (finish_reason={finish_reason}): {content}"
                )

            if not content:
                if attempt < max_attempts:
                    LOGGER.warning(
                        "LLM returned empty content; retrying (%s/%s)", attempt, max_attempts
                    )
                    hints = [_RETRY_INVALID_JSON_MESSAGE]
                    if attempt + 1 == max_attempts:
                        hints.append(_RETRY_MINIMAL_JSON_MESSAGE)
                    current_messages = _append_system_hints(messages, hints)
                    continue
                raise RuntimeError("LLM response is empty")

            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:  # pragma: no cover - passthrough
                if attempt < max_attempts:
                    LOGGER.warning(
                        "Failed to parse LLM JSON response on attempt %s/%s: %s",
                        attempt,
                        max_attempts,
                        exc,
                    )
                    hints = [_RETRY_INVALID_JSON_MESSAGE]
                    if attempt + 1 == max_attempts:
                        hints.append(_RETRY_MINIMAL_JSON_MESSAGE)
                    current_messages = _append_system_hints(messages, hints)
                    continue

                raise RuntimeError(f"Failed to parse LLM JSON response: {content}") from exc

        raise RuntimeError(
            f"Failed to produce valid JSON after {max_attempts} attempts: {last_content or ''}"
        )
