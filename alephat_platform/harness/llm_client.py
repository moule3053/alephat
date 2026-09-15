"""Harness LLM client: LiteLLM gateway (optional) or direct providers.

Modes (``LLM_MODE``):
  - ``auto`` (default): use LiteLLM when ``LITELLM_ENABLED=true`` or
    ``LITELLM_BASE_URL`` is set and reachable config says enabled; otherwise direct.
  - ``litellm``: always route through LiteLLM proxy.
  - ``direct``: always call provider APIs (OpenAI, Anthropic, Fireworks, Google).

Model id forms for direct mode:
  - ``openai:gpt-4o-mini`` / ``openai/gpt-4o-mini``
  - ``anthropic:claude-sonnet-4-20250514``
  - ``fireworks:accounts/fireworks/models/...``
  - ``google:gemini-2.0-flash``
  - bare name → ``DEFAULT_LLM_PROVIDER`` (default ``openai``)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from alephat_platform.common.config import get_settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = 5,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(attempts):
        try:
            response = await client.post(url, **kwargs)
        except httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError:
            if attempt + 1 >= attempts:
                raise
        else:
            if response.status_code not in RETRYABLE_STATUS_CODES or attempt + 1 >= attempts:
                return response
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = min(float(retry_after), 30.0)
                except ValueError:
                    delay = min(2**attempt, 10)
            else:
                delay = min(2**attempt, 10)
            logger.warning(
                "LLM request returned %s; retrying in %.1fs", response.status_code, delay
            )
            await asyncio.sleep(delay)
            continue
        delay = min(2**attempt, 10)
        logger.warning("LLM request failed to connect; retrying in %.1fs", delay)
        await asyncio.sleep(delay)
    raise RuntimeError("LLM retry loop exhausted")


class LLMClient(Protocol):
    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def message_content(self, response: dict[str, Any]) -> str: ...


def message_content_from_openai(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


@dataclass(frozen=True)
class ParsedModel:
    provider: str
    model: str


def parse_model_id(model_id: str, *, default_provider: str = "openai") -> ParsedModel:
    raw = (model_id or "").strip()
    if not raw:
        raise ValueError("model id is empty")
    if ":" in raw and not raw.startswith("http"):
        provider, _, name = raw.partition(":")
        return ParsedModel(provider=provider.strip().lower(), model=name.strip())
    if "/" in raw:
        provider, _, name = raw.partition("/")
        # fireworks uses paths like accounts/fireworks/models/x — treat as fireworks if prefix
        if provider.lower() in {
            "openai",
            "anthropic",
            "fireworks",
            "google",
            "gemini",
            "azure",
        }:
            return ParsedModel(provider=provider.strip().lower(), model=name.strip())
        # bare path with slashes → fireworks-style under default? keep full as model
        return ParsedModel(provider=default_provider.lower(), model=raw)
    return ParsedModel(provider=default_provider.lower(), model=raw)


class LiteLLMClient:
    """OpenAI-compatible client pointed at a LiteLLM proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.litellm_base_url or "http://localhost:4000").rstrip(
            "/"
        )
        self.api_key = api_key or settings.litellm_api_key or "sk-litellm"

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        # LiteLLM accepts logical names; strip provider prefix if present for routing aliases
        parsed = parse_model_id(model, default_provider="")
        model_name = parsed.model if parsed.provider else model
        # Prefer full id when user used provider:model and litellm is configured with that name
        if ":" in model or (parsed.provider and parsed.provider not in {"", "litellm"}):
            # Keep original if it looks like a litellm model_name; also try stripped
            model_name = model

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": model_name, "messages": messages, **kwargs}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(
                client,
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    def message_content(self, response: dict[str, Any]) -> str:
        return message_content_from_openai(response)


class DirectLLMClient:
    """Call provider HTTP APIs directly (no LiteLLM)."""

    def __init__(self) -> None:
        settings = get_settings()
        self.default_provider = settings.default_llm_provider
        self.openai_api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY")
        self.openai_base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        self.anthropic_api_key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.anthropic_base_url = (
            settings.anthropic_base_url or "https://api.anthropic.com"
        ).rstrip("/")
        self.fireworks_api_key = settings.fireworks_api_key or os.environ.get("FIREWORKS_API_KEY")
        self.fireworks_base_url = (
            settings.fireworks_base_url or "https://api.fireworks.ai/inference/v1"
        ).rstrip("/")
        self.google_api_key = settings.google_api_key or os.environ.get("GOOGLE_API_KEY")

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        parsed = parse_model_id(model, default_provider=self.default_provider)
        provider = parsed.provider
        if provider in {"gemini", "google_genai", "google-genai"}:
            provider = "google"

        if provider in {"openai", "azure"}:
            return await self._openai_compatible(
                base_url=self.openai_base_url,
                api_key=self.openai_api_key,
                model=parsed.model,
                messages=messages,
                provider_label="openai",
                **kwargs,
            )
        if provider == "fireworks":
            return await self._openai_compatible(
                base_url=self.fireworks_base_url,
                api_key=self.fireworks_api_key,
                model=parsed.model,
                messages=messages,
                provider_label="fireworks",
                **kwargs,
            )
        if provider == "anthropic":
            return await self._anthropic(
                model=parsed.model,
                messages=messages,
                **kwargs,
            )
        if provider == "google":
            return await self._google(
                model=parsed.model,
                messages=messages,
                **kwargs,
            )
        raise ValueError(
            f"Unsupported direct LLM provider '{provider}'. "
            f"Use openai|anthropic|fireworks|google or set LLM_MODE=litellm."
        )

    def message_content(self, response: dict[str, Any]) -> str:
        if response.get("_provider") == "anthropic":
            blocks = response.get("content") or []
            parts: list[str] = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
            return "".join(parts)
        if response.get("_provider") == "google":
            cands = response.get("candidates") or []
            if not cands:
                return ""
            parts = []
            for part in (cands[0].get("content") or {}).get("parts") or []:
                if isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
            return "".join(parts)
        return message_content_from_openai(response)

    async def _openai_compatible(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        messages: list[dict[str, Any]],
        provider_label: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not api_key:
            raise RuntimeError(
                f"{provider_label} API key is not set "
                f"(OPENAI_API_KEY / FIREWORKS_API_KEY / provider env)"
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": model, "messages": messages, **kwargs}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(
                client,
                f"{base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code >= 400:
                logger.error(
                    "%s chat error %s: %s", provider_label, resp.status_code, resp.text[:500]
                )
            resp.raise_for_status()
            data = resp.json()
            data["_provider"] = provider_label
            return data

    async def _anthropic(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        system_parts: list[str] = []
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role") or "user"
            content = msg.get("content") or ""
            if role == "system":
                system_parts.append(content if isinstance(content, str) else str(content))
                continue
            if role == "assistant":
                api_messages.append({"role": "assistant", "content": content})
            else:
                api_messages.append({"role": "user", "content": content})

        max_tokens = int(kwargs.pop("max_tokens", 4096) or 4096)
        body: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        # pass through temperature etc. if present
        for key in ("temperature", "top_p"):
            if key in kwargs:
                body[key] = kwargs[key]

        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(
                client,
                f"{self.anthropic_base_url}/v1/messages",
                headers=headers,
                json=body,
            )
            if resp.status_code >= 400:
                logger.error("anthropic error %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
            data["_provider"] = "anthropic"
            return data

    async def _google(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        # Convert to Gemini generateContent format
        system_instruction = None
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role") or "user"
            content = msg.get("content") or ""
            text = content if isinstance(content, str) else str(content)
            if role == "system":
                system_instruction = text
                continue
            gem_role = "model" if role == "assistant" else "user"
            contents.append({"role": gem_role, "parts": [{"text": text}]})

        body: dict[str, Any] = {"contents": contents}
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if "temperature" in kwargs:
            body["generationConfig"] = {"temperature": kwargs["temperature"]}

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.google_api_key}"
        )
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(client, url, json=body)
            if resp.status_code >= 400:
                logger.error("google error %s: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
            data["_provider"] = "google"
            return data


def resolve_llm_mode() -> str:
    """Return ``litellm`` or ``direct``."""
    settings = get_settings()
    mode = (settings.llm_mode or "auto").strip().lower()
    if mode in {"litellm", "gateway"}:
        return "litellm"
    if mode in {"direct", "provider", "providers"}:
        return "direct"
    # auto: explicit enable wins; else prefer direct providers when keys exist
    if settings.litellm_enabled:
        return "litellm"
    has_direct = bool(
        settings.openai_api_key
        or os.environ.get("OPENAI_API_KEY")
        or settings.anthropic_api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or settings.fireworks_api_key
        or os.environ.get("FIREWORKS_API_KEY")
        or settings.google_api_key
        or os.environ.get("GOOGLE_API_KEY")
    )
    if has_direct:
        return "direct"
    if settings.litellm_base_url:
        return "litellm"
    return "direct"


def make_llm_client() -> LLMClient:
    mode = resolve_llm_mode()
    logger.info("Harness LLM mode: %s", mode)
    if mode == "litellm":
        return LiteLLMClient()
    return DirectLLMClient()


# Back-compat alias
def LiteLLMClientFactory() -> LLMClient:  # noqa: N802
    return make_llm_client()
