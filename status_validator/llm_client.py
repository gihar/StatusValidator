from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from openai import OpenAI
from openai import APIError

from .config import LLMConfig, LLMProviderConfig


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


def _is_reasoning_unsupported_error(error: BaseException) -> bool:
    """Heuristic to detect when a provider rejects the reasoning parameter."""

    message = str(error).lower()
    if "reasoning" not in message:
        return False

    keywords = ("unsupported", "unknown", "not allowed", "invalid", "cannot")
    return any(keyword in message for keyword in keywords)


def _is_prompt_cache_key_unsupported_error(error: BaseException) -> bool:
    """Heuristic to detect when a provider rejects the prompt_cache_key parameter."""

    message = str(error).lower()
    if "prompt_cache_key" not in message:
        return False

    keywords = ("unsupported", "unknown", "not allowed", "invalid", "cannot")
    return any(keyword in message for keyword in keywords)


def _append_system_hints(messages: List[Dict[str, str]], hints: List[str]) -> List[Dict[str, str]]:
    """Return a fresh copy of messages extended with additional system hints."""

    updated = [msg.copy() for msg in messages]
    for hint in hints:
        updated.append({"role": "system", "content": hint})
    return updated


@dataclass
class _ProviderClient:
    priority: int
    config: LLMProviderConfig
    client: OpenAI
    model_name: str
    display_name: str

    @property
    def temperature(self) -> float:
        return self.config.temperature

    @property
    def max_output_tokens(self) -> int:
        return self.config.max_output_tokens

    @property
    def request_timeout(self) -> int:
        return self.config.request_timeout


class LLMClient:
    """Wrapper around OpenAI client with JSON enforcement and provider fallbacks."""

    def __init__(self, conf: LLMConfig) -> None:
        self._conf = conf
        self._providers: List[_ProviderClient] = []
        self._last_model_name: str | None = None

        for priority, provider_conf in conf.provider_sequence:
            provider = self._build_provider(priority, provider_conf)
            if provider is not None:
                self._providers.append(provider)

        if not self._providers:
            msg = (
                "No valid LLM providers configured. Ensure that API keys and model identifiers "
                "are provided in the configuration or environment."
            )
            raise RuntimeError(msg)

    @property
    def model_name(self) -> str:
        """Return the model identifier used by the most recent call."""

        if self._last_model_name:
            return self._last_model_name
        return self._providers[0].model_name

    def generate(
        self, 
        messages: List[Dict[str, str]], 
        prompt_cache_key: str | None = None
    ) -> Dict[str, Any]:
        """Call the chat completion endpoint and return parsed JSON content.
        
        Args:
            messages: Chat messages to send to the LLM
            prompt_cache_key: Optional cache key to improve cache hit rates
        """

        last_error_messages: List[Tuple[str, str]] = []

        for provider in self._providers:
            try:
                payload = self._generate_with_provider(provider, messages, prompt_cache_key)
            except RuntimeError as exc:  # pragma: no cover - passthrough for logging
                LOGGER.warning(
                    "LLM provider '%s' failed after %s attempts: %s",
                    provider.display_name,
                    self._conf.max_retries,
                    exc,
                )
                last_error_messages.append((provider.display_name, str(exc)))
                continue

            self._last_model_name = provider.model_name
            return payload

        errors_joined = ", ".join(
            f"{name}: {message}" for name, message in last_error_messages
        ) or "no providers produced a usable response"
        raise RuntimeError(f"All LLM providers failed: {errors_joined}")

    def _build_provider(
        self,
        priority: int,
        provider_conf: LLMProviderConfig,
    ) -> _ProviderClient | None:
        display_name = provider_conf.name or f"provider-{priority}"

        api_key = provider_conf.api_key
        if not api_key and provider_conf.api_key_env:
            api_key = os.environ.get(provider_conf.api_key_env)
        if not api_key:
            LOGGER.warning(
                "Skipping LLM provider '%s': API key is not configured",
                display_name,
            )
            return None

        model_name = provider_conf.model
        if not model_name and provider_conf.model_env:
            model_name = os.environ.get(provider_conf.model_env)
        if not model_name:
            LOGGER.warning(
                "Skipping LLM provider '%s': model identifier is not configured",
                display_name,
            )
            return None

        base_url = provider_conf.base_url
        if not base_url and provider_conf.base_url_env:
            base_url = os.environ.get(provider_conf.base_url_env)

        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "organization": provider_conf.organization,
        }

        default_headers: Dict[str, str] = {}
        if self._conf.http_referer:
            default_headers["HTTP-Referer"] = self._conf.http_referer
        if self._conf.x_title:
            default_headers["X-Title"] = self._conf.x_title
        if default_headers:
            client_kwargs["default_headers"] = default_headers

        client = OpenAI(**client_kwargs)

        return _ProviderClient(
            priority=priority,
            config=provider_conf,
            client=client,
            model_name=model_name,
            display_name=display_name,
        )

    def _generate_with_provider(
        self,
        provider: _ProviderClient,
        messages: List[Dict[str, str]],
        prompt_cache_key: str | None = None,
    ) -> Dict[str, Any]:
        max_attempts = self._conf.max_retries
        current_messages = [msg.copy() for msg in messages]
        last_content: str | None = None
        reasoning_enabled = provider.config.reasoning_enabled
        prompt_cache_enabled = prompt_cache_key is not None

        for attempt in range(1, max_attempts + 1):
            # Prepare API call parameters
            api_params = {
                "model": provider.model_name,
                "messages": current_messages,
                "temperature": provider.temperature,
                "max_tokens": provider.max_output_tokens,
                "response_format": {"type": "json_object"},
                "timeout": provider.request_timeout,
            }

            extra_body: Dict[str, Any] = {}
            if reasoning_enabled:
                extra_body["reasoning"] = {"effort": "high"}

            # Add prompt_cache_key if provided and enabled (helps OpenAI optimize caching)
            if prompt_cache_key and prompt_cache_enabled:
                extra_body["prompt_cache_key"] = prompt_cache_key
                if attempt == 1:
                    LOGGER.debug("Using prompt_cache_key: %s", prompt_cache_key)

            if extra_body:
                api_params["extra_body"] = extra_body

            try:
                response = provider.client.chat.completions.create(**api_params)
            except json.JSONDecodeError as exc:
                if reasoning_enabled:
                    LOGGER.warning(
                        "Provider '%s' returned a non-JSON response with reasoning enabled; retrying without reasoning (%s)",
                        provider.display_name,
                        exc,
                    )
                    reasoning_enabled = False
                    continue
                raise RuntimeError("LLM response could not be parsed as JSON") from exc
            except APIError as exc:  # pragma: no cover - passthrough
                if reasoning_enabled and _is_reasoning_unsupported_error(exc):
                    LOGGER.warning(
                        "Disabling reasoning for provider '%s' due to error: %s",
                        provider.display_name,
                        exc,
                    )
                    reasoning_enabled = False
                    continue
                if prompt_cache_enabled and _is_prompt_cache_key_unsupported_error(exc):
                    LOGGER.warning(
                        "Disabling prompt_cache_key for provider '%s' due to error: %s",
                        provider.display_name,
                        exc,
                    )
                    prompt_cache_enabled = False
                    continue
                raise RuntimeError(f"LLM request failed: {exc}") from exc
            except Exception as exc:  # pragma: no cover - passthrough
                if reasoning_enabled and "reasoning" in str(exc).lower():
                    LOGGER.warning(
                        "Disabling reasoning for provider '%s' due to unexpected error: %s",
                        provider.display_name,
                        exc,
                    )
                    reasoning_enabled = False
                    continue
                if prompt_cache_enabled and "prompt_cache_key" in str(exc).lower():
                    LOGGER.warning(
                        "Disabling prompt_cache_key for provider '%s' due to unexpected error: %s",
                        provider.display_name,
                        exc,
                    )
                    prompt_cache_enabled = False
                    continue
                raise RuntimeError(f"LLM request failed: {exc}") from exc

            # Log prompt caching metrics if available (OpenAI GPT-4o, o1, etc.)
            if hasattr(response, "usage") and response.usage:
                usage = response.usage
                total_tokens = getattr(usage, "total_tokens", 0)
                prompt_tokens = getattr(usage, "prompt_tokens", 0)
                
                # Check for cached tokens (OpenAI automatic prompt caching)
                if hasattr(usage, "prompt_tokens_details"):
                    details = usage.prompt_tokens_details
                    cached_tokens = getattr(details, "cached_tokens", 0)
                    if cached_tokens > 0:
                        cache_hit_rate = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0
                        LOGGER.info(
                            "Prompt cache hit: %d/%d tokens (%.1f%%) | Total: %d tokens",
                            cached_tokens,
                            prompt_tokens,
                            cache_hit_rate,
                            total_tokens,
                        )
                    else:
                        LOGGER.debug("No cached tokens in this request (total: %d tokens)", total_tokens)

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
