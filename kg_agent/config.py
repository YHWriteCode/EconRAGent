from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass
class AgentModelConfig:
    provider: str = "openai_compatible"
    model_name: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> "AgentModelConfig":
        return cls(
            provider=os.getenv("KG_AGENT_MODEL_PROVIDER", "openai_compatible"),
            model_name=os.getenv(
                "KG_AGENT_MODEL_NAME",
                os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "")),
            ),
            base_url=os.getenv(
                "KG_AGENT_MODEL_BASE_URL",
                os.getenv("OPENAI_BASE_URL", os.getenv("LLM_BINDING_HOST", "")),
            ),
            api_key=os.getenv(
                "KG_AGENT_MODEL_API_KEY",
                os.getenv("OPENAI_API_KEY", os.getenv("LLM_BINDING_API_KEY", "")),
            ),
            timeout_s=_env_float("KG_AGENT_MODEL_TIMEOUT_S", 60.0),
        )

    def is_configured(self) -> bool:
        return bool(self.model_name and self.base_url)


@dataclass
class ToolConfig:
    enable_web_search: bool = False
    enable_memory: bool = True
    enable_quant: bool = True
    enable_kg_ingest: bool = True

    @classmethod
    def from_env(cls) -> "ToolConfig":
        return cls(
            enable_web_search=_env_bool("KG_AGENT_ENABLE_WEB_SEARCH", False),
            enable_memory=_env_bool("KG_AGENT_ENABLE_MEMORY", True),
            enable_quant=_env_bool("KG_AGENT_ENABLE_QUANT", True),
            enable_kg_ingest=_env_bool("KG_AGENT_ENABLE_KG_INGEST", True),
        )


@dataclass
class CrawlerConfig:
    provider: str = "crawl4ai"
    browser_type: str = "chromium"
    browser_channel: str = ""
    search_engine: str = "duckduckgo"
    headless: bool = True
    verbose: bool = False
    cache_mode: str = "BYPASS"
    max_pages: int = 3
    max_content_chars: int = 4000
    word_count_threshold: int = 20
    page_timeout_ms: int = 30000

    @classmethod
    def from_env(cls) -> "CrawlerConfig":
        return cls(
            provider=os.getenv("KG_AGENT_WEB_CRAWLER_PROVIDER", "crawl4ai"),
            browser_type=os.getenv("KG_AGENT_WEB_CRAWLER_BROWSER_TYPE", "chromium"),
            browser_channel=os.getenv("KG_AGENT_WEB_CRAWLER_BROWSER_CHANNEL", "").strip(),
            search_engine=os.getenv("KG_AGENT_WEB_SEARCH_ENGINE", "duckduckgo"),
            headless=_env_bool("KG_AGENT_WEB_CRAWLER_HEADLESS", True),
            verbose=_env_bool("KG_AGENT_WEB_CRAWLER_VERBOSE", False),
            cache_mode=os.getenv("KG_AGENT_WEB_CRAWLER_CACHE_MODE", "BYPASS"),
            max_pages=_env_int("KG_AGENT_WEB_CRAWLER_MAX_PAGES", 3),
            max_content_chars=_env_int("KG_AGENT_WEB_CRAWLER_MAX_CONTENT_CHARS", 4000),
            word_count_threshold=_env_int(
                "KG_AGENT_WEB_CRAWLER_WORD_COUNT_THRESHOLD", 20
            ),
            page_timeout_ms=_env_int(
                "KG_AGENT_WEB_CRAWLER_PAGE_TIMEOUT_MS", 30000
            ),
        )


@dataclass
class AgentRuntimeConfig:
    default_workspace: str = ""
    default_domain_schema: str = "general"
    max_iterations: int = 3
    memory_window_turns: int = 6
    debug: bool = False

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        return cls(
            default_workspace=os.getenv(
                "KG_AGENT_DEFAULT_WORKSPACE", os.getenv("WORKSPACE", "")
            ),
            default_domain_schema=os.getenv(
                "KG_AGENT_DEFAULT_DOMAIN_SCHEMA", "general"
            ),
            max_iterations=_env_int("KG_AGENT_MAX_ITERATIONS", 3),
            memory_window_turns=_env_int("KG_AGENT_MEMORY_WINDOW_TURNS", 6),
            debug=_env_bool("KG_AGENT_DEBUG", False),
        )


@dataclass
class KGAgentConfig:
    agent_model: AgentModelConfig = field(default_factory=AgentModelConfig)
    tool_config: ToolConfig = field(default_factory=ToolConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    runtime: AgentRuntimeConfig = field(default_factory=AgentRuntimeConfig)
    judge_model: AgentModelConfig | None = None
    answer_model: AgentModelConfig | None = None
    path_model: AgentModelConfig | None = None

    @classmethod
    def from_env(cls) -> "KGAgentConfig":
        return cls(
            agent_model=AgentModelConfig.from_env(),
            tool_config=ToolConfig.from_env(),
            crawler=CrawlerConfig.from_env(),
            runtime=AgentRuntimeConfig.from_env(),
        )


class AgentLLMClient:
    """OpenAI-compatible LLM client for the business-layer agent."""

    def __init__(self, config: AgentModelConfig):
        self.config = config
        self._client: Any | None = None
        self._disabled_reason: str | None = None

    def is_available(self) -> bool:
        return self.config.is_configured() and self.config.provider != "disabled"

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    def _ensure_client(self) -> Any:
        if not self.is_available():
            self._disabled_reason = "Agent model is not configured"
            raise RuntimeError(self._disabled_reason)

        if self._client is not None:
            return self._client

        if self.config.provider not in {"openai_compatible", "openai"}:
            self._disabled_reason = (
                f"Unsupported KG agent model provider: {self.config.provider}"
            )
            raise RuntimeError(self._disabled_reason)

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            self._disabled_reason = "openai package is not installed"
            raise RuntimeError(self._disabled_reason) from exc

        self._client = AsyncOpenAI(
            api_key=self.config.api_key or "EMPTY",
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
        )
        return self._client

    async def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str:
        client = self._ensure_client()
        response = await client.chat.completions.create(
            model=self.config.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        message = response.choices[0].message
        return message.content or ""

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        raw_text = await self.complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        payload = raw_text.strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.lower().startswith("json"):
                payload = payload[4:].strip()
        start = payload.find("{")
        end = payload.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM did not return a JSON object: {raw_text}")
        return json.loads(payload[start : end + 1])
