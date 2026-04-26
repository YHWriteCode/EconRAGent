from __future__ import annotations

import ast
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import AsyncIterator

from dotenv import load_dotenv

def _find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "kg_agent").is_dir():
            return candidate
    return Path.cwd().resolve()


PROJECT_ROOT = _find_project_root()


def load_project_dotenv(*, override: bool = False) -> Path | None:
    configured = os.getenv("KG_AGENT_DOTENV_PATH", "").strip()
    dotenv_path = Path(configured).expanduser() if configured else PROJECT_ROOT / ".env"
    if not dotenv_path.is_absolute():
        dotenv_path = PROJECT_ROOT / dotenv_path
    if not dotenv_path.is_file():
        fallback = Path.cwd().resolve() / ".env"
        dotenv_path = fallback if fallback.is_file() else dotenv_path
    if dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=override)
        os.environ.setdefault("KG_AGENT_DOTENV_PATH", str(dotenv_path))
        os.environ.setdefault("KG_AGENT_PROJECT_ROOT", str(dotenv_path.parent))
        return dotenv_path
    os.environ.setdefault("KG_AGENT_PROJECT_ROOT", str(PROJECT_ROOT))
    return None


def resolve_project_path(path: str | os.PathLike[str]) -> Path:
    raw = str(path).strip()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    root = Path(os.getenv("KG_AGENT_PROJECT_ROOT", str(PROJECT_ROOT))).resolve()
    return (root / candidate).resolve()


def _normalize_docker_volume_arg(arg: str) -> str:
    value = arg.strip()
    if not value.startswith(("./", ".\\")):
        return arg
    host, separator, rest = value.partition(":")
    if not separator:
        return arg
    resolved_host = str(resolve_project_path(host)).replace("\\", "/")
    return f"{resolved_host}:{rest}"


load_project_dotenv(override=False)
logger = logging.getLogger(__name__)
JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)```",
    re.IGNORECASE | re.DOTALL,
)
TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")
SMART_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
    }
)


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


def _first_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return ""


def _first_env_float(default: float, *names: str) -> float:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return float(value)
        except ValueError:
            continue
    return default


def _env_json(name: str, default: Any) -> Any:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip()
    if not normalized:
        return default
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s; falling back to default.", name)
        return default


def _env_csv(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default)
    normalized = value.strip()
    if not normalized:
        return list(default)
    if normalized == "*":
        return ["*"]
    parts = [item.strip() for item in normalized.split(",") if item.strip()]
    return parts or list(default)


def _strip_json_fences(payload: str) -> str:
    normalized = str(payload or "").strip()
    match = JSON_FENCE_PATTERN.search(normalized)
    if match:
        return str(match.group("body") or "").strip()
    if normalized.startswith("```"):
        return normalized.strip("`").strip()
    return normalized


def _extract_json_object_text(payload: str) -> str:
    normalized = _strip_json_fences(payload)
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM did not return a JSON object: {payload}")
    return normalized[start : end + 1]


def _normalize_json_like_text(payload: str) -> str:
    normalized = str(payload or "").translate(SMART_QUOTE_TRANSLATION)
    normalized = TRAILING_COMMA_PATTERN.sub(r"\1", normalized)
    return normalized


def _coerce_mapping_to_plain_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object, received: {type(value).__name__}")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _parse_json_like_object(payload: str) -> dict[str, Any]:
    object_text = _extract_json_object_text(payload)
    candidates: list[str] = []
    for item in (object_text, _normalize_json_like_text(object_text)):
        if item and item not in candidates:
            candidates.append(item)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _coerce_mapping_to_plain_dict(json.loads(candidate))
        except Exception as exc:
            last_error = exc
        try:
            return _coerce_mapping_to_plain_dict(ast.literal_eval(candidate))
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError(f"LLM did not return a JSON object: {payload}")


def _default_skill_runtime_target():
    from kg_agent.skills.models import SkillRuntimeTarget

    return SkillRuntimeTarget.linux_default()


def _default_crawler_llm_provider() -> str:
    explicit_provider = _first_env_value(
        "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_PROVIDER",
    )
    if explicit_provider:
        return explicit_provider
    model_name = _first_env_value(
        "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_MODEL",
        "KG_AGENT_MODEL_NAME",
        "LLM_MODEL_NAME",
        "LLM_MODEL",
    )
    if not model_name:
        return ""
    return f"openai/{model_name}"


@dataclass
class AgentModelConfig:
    provider: str = "openai_compatible"
    model_name: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_s: float = 60.0

    @classmethod
    def from_env_keys(
        cls,
        *,
        provider_keys: tuple[str, ...],
        model_keys: tuple[str, ...],
        base_url_keys: tuple[str, ...],
        api_key_keys: tuple[str, ...],
        timeout_keys: tuple[str, ...],
        default_provider: str = "openai_compatible",
        default_timeout: float = 60.0,
    ) -> "AgentModelConfig":
        return cls(
            provider=_first_env_value(*provider_keys) or default_provider,
            model_name=_first_env_value(*model_keys),
            base_url=_first_env_value(*base_url_keys),
            api_key=_first_env_value(*api_key_keys),
            timeout_s=_first_env_float(default_timeout, *timeout_keys),
        )

    @classmethod
    def from_env(cls) -> "AgentModelConfig":
        return cls.from_env_keys(
            provider_keys=("KG_AGENT_MODEL_PROVIDER",),
            model_keys=("KG_AGENT_MODEL_NAME", "LLM_MODEL", "OPENAI_MODEL"),
            base_url_keys=(
                "KG_AGENT_MODEL_BASE_URL",
                "OPENAI_BASE_URL",
                "LLM_BINDING_HOST",
            ),
            api_key_keys=(
                "KG_AGENT_MODEL_API_KEY",
                "OPENAI_API_KEY",
                "LLM_BINDING_API_KEY",
            ),
            timeout_keys=("KG_AGENT_MODEL_TIMEOUT_S",),
        )

    @classmethod
    def from_utility_env(cls) -> "AgentModelConfig":
        return cls.from_env_keys(
            provider_keys=(
                "KG_AGENT_UTILITY_MODEL_PROVIDER",
                "UTILITY_LLM_PROVIDER",
            ),
            model_keys=(
                "KG_AGENT_UTILITY_MODEL_NAME",
                "UTILITY_LLM_MODEL",
            ),
            base_url_keys=(
                "KG_AGENT_UTILITY_MODEL_BASE_URL",
                "UTILITY_LLM_BINDING_HOST",
            ),
            api_key_keys=(
                "KG_AGENT_UTILITY_MODEL_API_KEY",
                "UTILITY_LLM_BINDING_API_KEY",
            ),
            timeout_keys=(
                "KG_AGENT_UTILITY_MODEL_TIMEOUT_S",
                "UTILITY_LLM_TIMEOUT",
            ),
        )

    def is_configured(self) -> bool:
        return bool(
            self.model_name
            and self.base_url
            and self.provider in {"openai_compatible", "openai"}
        )


@dataclass
class ToolConfig:
    enable_web_search: bool = False
    enable_memory: bool = True
    enable_kg_ingest: bool = True

    @classmethod
    def from_env(cls) -> "ToolConfig":
        return cls(
            enable_web_search=_env_bool("KG_AGENT_ENABLE_WEB_SEARCH", False),
            enable_memory=_env_bool("KG_AGENT_ENABLE_MEMORY", True),
            enable_kg_ingest=_env_bool("KG_AGENT_ENABLE_KG_INGEST", True),
        )


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    stdio_framing: str = "auto"
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout_s: float = 15.0
    tool_timeout_s: float = 60.0
    discover_tools: bool = False

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "MCPServerConfig | None":
        name = str(payload.get("name", "")).strip()
        command = str(payload.get("command", "")).strip()
        if not name or not command:
            return None
        args = payload.get("args", [])
        env = payload.get("env", {})
        normalized_env = (
            {
                str(key): str(value)
                for key, value in env.items()
            }
            if isinstance(env, dict)
            else {}
        )
        normalized_args = [
            str(item)
            for item in args
            if isinstance(item, (str, int, float))
        ]
        if command == "docker":
            normalized_args = [
                _normalize_docker_volume_arg(item)
                if index > 0 and normalized_args[index - 1] in {"-v", "--volume"}
                else item
                for index, item in enumerate(normalized_args)
            ]
        return cls(
            name=name,
            transport=str(payload.get("transport", "stdio")).strip() or "stdio",
            command=command,
            args=normalized_args,
            stdio_framing=str(payload.get("stdio_framing", "auto")).strip() or "auto",
            env=normalized_env,
            startup_timeout_s=float(payload.get("startup_timeout_s", 15.0)),
            tool_timeout_s=float(payload.get("tool_timeout_s", 60.0)),
            discover_tools=bool(payload.get("discover_tools", False)),
        )


@dataclass
class MCPCapabilityConfig:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    remote_name: str = ""
    enabled: bool = True
    planner_exposed: bool = False
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "MCPCapabilityConfig | None":
        name = str(payload.get("name", "")).strip()
        description = str(payload.get("description", "")).strip()
        server = str(payload.get("server", "")).strip()
        input_schema = payload.get("input_schema", {})
        if not name or not description or not server or not isinstance(input_schema, dict):
            return None
        tags = payload.get("tags", [])
        return cls(
            name=name,
            description=description,
            input_schema=input_schema,
            server=server,
            remote_name=str(payload.get("remote_name", "")).strip(),
            enabled=bool(payload.get("enabled", True)),
            planner_exposed=bool(payload.get("planner_exposed", False)),
            tags=[str(item) for item in tags if isinstance(item, str)],
        )


@dataclass
class MCPConfig:
    servers: list[MCPServerConfig] = field(default_factory=list)
    capabilities: list[MCPCapabilityConfig] = field(default_factory=list)

    def discovery_enabled(self) -> bool:
        return any(server.discover_tools for server in self.servers)

    def is_configured(self) -> bool:
        return bool(self.servers)

    @classmethod
    def from_env(cls) -> "MCPConfig":
        raw_servers = _env_json("KG_AGENT_MCP_SERVERS_JSON", [])
        raw_capabilities = _env_json("KG_AGENT_MCP_CAPABILITIES_JSON", [])
        servers = []
        capabilities = []
        if isinstance(raw_servers, list):
            for item in raw_servers:
                if not isinstance(item, dict):
                    continue
                parsed = MCPServerConfig.from_mapping(item)
                if parsed is not None:
                    servers.append(parsed)
        if isinstance(raw_capabilities, list):
            for item in raw_capabilities:
                if not isinstance(item, dict):
                    continue
                parsed = MCPCapabilityConfig.from_mapping(item)
                if parsed is not None:
                    capabilities.append(parsed)
        return cls(servers=servers, capabilities=capabilities)


@dataclass
class SkillRuntimeConfig:
    server: str = ""
    run_tool_name: str = "run_skill_task"
    status_tool_name: str = "get_run_status"
    cancel_tool_name: str = "cancel_skill_run"
    read_tool_name: str = "read_skill"
    read_file_tool_name: str = "read_skill_file"
    logs_tool_name: str = "get_run_logs"
    artifacts_tool_name: str = "get_run_artifacts"
    default_shell_mode: str = "conservative"
    wait_for_terminal_result: bool = True
    wait_for_terminal_timeout_s: float = 30.0
    wait_for_terminal_poll_interval_s: float = 1.0
    default_runtime_target: SkillRuntimeTarget = field(
        default_factory=_default_skill_runtime_target
    )

    def is_configured(self) -> bool:
        return bool(self.server.strip())

    @classmethod
    def from_env(cls) -> "SkillRuntimeConfig":
        from kg_agent.skills.models import SkillRuntimeTarget

        raw_artifacts_tool_name = os.getenv(
            "KG_AGENT_SKILL_RUNTIME_ARTIFACTS_TOOL",
            "get_run_artifacts",
        ).strip()
        artifacts_tool_name = (
            ""
            if raw_artifacts_tool_name.lower()
            in {"disabled", "host_workspace", "host_workspace_fallback"}
            else raw_artifacts_tool_name or "get_run_artifacts"
        )

        return cls(
            server=os.getenv("KG_AGENT_SKILL_RUNTIME_SERVER", "").strip(),
            run_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_RUN_TOOL",
                "run_skill_task",
            ).strip()
            or "run_skill_task",
            status_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_STATUS_TOOL",
                "get_run_status",
            ).strip()
            or "get_run_status",
            cancel_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_CANCEL_TOOL",
                "cancel_skill_run",
            ).strip()
            or "cancel_skill_run",
            read_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_READ_TOOL",
                "read_skill",
            ).strip()
            or "read_skill",
            read_file_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_READ_FILE_TOOL",
                "read_skill_file",
            ).strip()
            or "read_skill_file",
            logs_tool_name=os.getenv(
                "KG_AGENT_SKILL_RUNTIME_LOGS_TOOL",
                "get_run_logs",
            ).strip()
            or "get_run_logs",
            artifacts_tool_name=artifacts_tool_name,
            default_shell_mode=os.getenv(
                "KG_AGENT_SKILL_DEFAULT_SHELL_MODE",
                "conservative",
            ).strip()
            or "conservative",
            wait_for_terminal_result=_env_bool(
                "KG_AGENT_SKILL_WAIT_FOR_TERMINAL_RESULT",
                True,
            ),
            wait_for_terminal_timeout_s=_env_float(
                "KG_AGENT_SKILL_WAIT_FOR_TERMINAL_TIMEOUT_S",
                30.0,
            ),
            wait_for_terminal_poll_interval_s=_env_float(
                "KG_AGENT_SKILL_WAIT_FOR_TERMINAL_POLL_INTERVAL_S",
                1.0,
            ),
            default_runtime_target=SkillRuntimeTarget.from_dict(
                {
                    "platform": os.getenv(
                        "KG_AGENT_SKILL_TARGET_PLATFORM",
                        "linux",
                    ),
                    "shell": os.getenv(
                        "KG_AGENT_SKILL_TARGET_SHELL",
                        "/bin/sh",
                    ),
                    "workspace_root": os.getenv(
                        "KG_AGENT_SKILL_TARGET_WORKSPACE_ROOT",
                        "/workspace",
                    ),
                    "workdir": os.getenv(
                        "KG_AGENT_SKILL_TARGET_WORKDIR",
                        "/workspace",
                    ),
                    "network_allowed": _env_bool(
                        "KG_AGENT_SKILL_TARGET_NETWORK_ALLOWED",
                        False,
                    ),
                    "supports_python": _env_bool(
                        "KG_AGENT_SKILL_TARGET_SUPPORTS_PYTHON",
                        True,
                    ),
                }
            ),
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
    llm_extraction_enabled: bool = False
    llm_extraction_provider: str = ""
    llm_extraction_api_token: str = ""
    llm_extraction_base_url: str = ""
    llm_extraction_instruction: str = ""
    llm_extraction_input_format: str = "markdown"
    llm_extraction_type: str = "block"
    llm_extraction_schema_json: str = ""
    llm_extraction_force_json_response: bool = False
    llm_extraction_apply_chunking: bool = True
    llm_extraction_prefer_content: bool = False

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
            llm_extraction_enabled=_env_bool(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_ENABLED", False
            ),
            llm_extraction_provider=_default_crawler_llm_provider(),
            llm_extraction_api_token=_first_env_value(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_API_TOKEN",
                "LLM_BINDING_API_KEY",
                "OPENAI_API_KEY",
            ),
            llm_extraction_base_url=_first_env_value(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_BASE_URL",
                "LLM_BINDING_HOST",
                "OPENAI_API_BASE",
            ),
            llm_extraction_instruction=os.getenv(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_INSTRUCTION", ""
            ).strip(),
            llm_extraction_input_format=os.getenv(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_INPUT_FORMAT", "markdown"
            ).strip()
            or "markdown",
            llm_extraction_type=os.getenv(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_TYPE", "block"
            ).strip()
            or "block",
            llm_extraction_schema_json=os.getenv(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_SCHEMA_JSON", ""
            ).strip(),
            llm_extraction_force_json_response=_env_bool(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_FORCE_JSON_RESPONSE", False
            ),
            llm_extraction_apply_chunking=_env_bool(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_APPLY_CHUNKING", True
            ),
            llm_extraction_prefer_content=_env_bool(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_PREFER_CONTENT", False
            ),
        )


@dataclass
class SchedulerConfig:
    enable_scheduler: bool = False
    check_interval_seconds: int = 60
    sources_file: str = ""
    state_file: str = "scheduler_state.json"
    coordination_backend: str = "auto"
    coordination_redis_url: str = ""
    coordination_ttl_seconds: int = 120
    enable_leader_election: bool = False
    loop_lease_key: str = "scheduler:loop"

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        return cls(
            enable_scheduler=_env_bool("KG_AGENT_ENABLE_SCHEDULER", False),
            check_interval_seconds=_env_int(
                "KG_AGENT_SCHEDULER_CHECK_INTERVAL", 60
            ),
            sources_file=os.getenv("KG_AGENT_SCHEDULER_SOURCES_FILE", "").strip(),
            state_file=os.getenv(
                "KG_AGENT_SCHEDULER_STATE_FILE", "scheduler_state.json"
            ).strip()
            or "scheduler_state.json",
            coordination_backend=os.getenv(
                "KG_AGENT_SCHEDULER_COORDINATION_BACKEND", "auto"
            ).strip()
            or "auto",
            coordination_redis_url=os.getenv(
                "KG_AGENT_SCHEDULER_COORDINATION_REDIS_URL",
                os.getenv("REDIS_URI", ""),
            ).strip(),
            coordination_ttl_seconds=_env_int(
                "KG_AGENT_SCHEDULER_COORDINATION_TTL_SECONDS", 120
            ),
            enable_leader_election=_env_bool(
                "KG_AGENT_SCHEDULER_ENABLE_LEADER_ELECTION", False
            ),
            loop_lease_key=os.getenv(
                "KG_AGENT_SCHEDULER_LOOP_LEASE_KEY", "scheduler:loop"
            ).strip()
            or "scheduler:loop",
        )


@dataclass
class FreshnessConfig:
    threshold_seconds: int = 604800
    enable_auto_ingest: bool = False
    staleness_decay_days: float = 7.0
    enable_freshness_decay: bool = False

    @classmethod
    def from_env(cls) -> "FreshnessConfig":
        return cls(
            threshold_seconds=_env_int(
                "KG_AGENT_FRESHNESS_THRESHOLD_SECONDS", 604800
            ),
            enable_auto_ingest=_env_bool("KG_AGENT_ENABLE_AUTO_INGEST", False),
            staleness_decay_days=_env_float(
                "KG_AGENT_STALENESS_DECAY_DAYS", 7.0
            ),
            enable_freshness_decay=_env_bool(
                "KG_AGENT_ENABLE_FRESHNESS_DECAY", False
            ),
        )


@dataclass
class PersistenceConfig:
    memory_backend: str = "memory"
    memory_sqlite_path: str = "kg_agent_memory.sqlite3"
    memory_mongo_collection: str = "kg_agent_conversation_messages"
    user_profile_backend: str = "memory"
    user_profile_sqlite_path: str = "kg_agent_profiles.sqlite3"
    user_profile_mongo_collection: str = "kg_agent_user_profiles"
    scheduler_store_backend: str = "json"
    scheduler_store_sqlite_path: str = "kg_agent_scheduler.sqlite3"
    workspace_registry_file: str = "kg_agent_workspaces.json"
    workspace_registry_sqlite_path: str = "kg_agent_workspaces.sqlite3"
    uploads_dir: str = "kg_agent_uploads"
    cross_session_backend: str = "memory"
    cross_session_mongo_collection: str = "kg_agent_cross_session_messages"
    cross_session_qdrant_collection_prefix: str = "kg_agent_cross_session"
    cross_session_min_content_chars: int = 8
    cross_session_max_content_chars: int = 1200
    cross_session_max_session_refs: int = 12
    cross_session_enable_consolidation: bool = True
    cross_session_consolidation_similarity_threshold: float = 0.82
    cross_session_consolidation_top_k: int = 3
    cross_session_max_cluster_snippets: int = 6
    cross_session_enable_background_maintenance: bool = False
    cross_session_maintenance_interval_seconds: int = 1800
    cross_session_maintenance_batch_size: int = 100
    cross_session_aging_stale_after_days: float = 14.0
    cross_session_aging_delete_after_days: float = 60.0
    cross_session_aging_keep_min_occurrences: int = 2
    cross_session_aging_max_snippets: int = 3

    @classmethod
    def from_env(cls) -> "PersistenceConfig":
        return cls(
            memory_backend=os.getenv("KG_AGENT_MEMORY_BACKEND", "memory").strip()
            or "memory",
            memory_sqlite_path=os.getenv(
                "KG_AGENT_MEMORY_SQLITE_PATH", "kg_agent_memory.sqlite3"
            ).strip()
            or "kg_agent_memory.sqlite3",
            memory_mongo_collection=os.getenv(
                "KG_AGENT_MEMORY_MONGO_COLLECTION",
                "kg_agent_conversation_messages",
            ).strip()
            or "kg_agent_conversation_messages",
            user_profile_backend=os.getenv(
                "KG_AGENT_USER_PROFILE_BACKEND", "memory"
            ).strip()
            or "memory",
            user_profile_sqlite_path=os.getenv(
                "KG_AGENT_USER_PROFILE_SQLITE_PATH", "kg_agent_profiles.sqlite3"
            ).strip()
            or "kg_agent_profiles.sqlite3",
            user_profile_mongo_collection=os.getenv(
                "KG_AGENT_USER_PROFILE_MONGO_COLLECTION",
                "kg_agent_user_profiles",
            ).strip()
            or "kg_agent_user_profiles",
            scheduler_store_backend=os.getenv(
                "KG_AGENT_SCHEDULER_STORE_BACKEND", "json"
            ).strip()
            or "json",
            scheduler_store_sqlite_path=os.getenv(
                "KG_AGENT_SCHEDULER_STORE_SQLITE_PATH",
                "kg_agent_scheduler.sqlite3",
            ).strip()
            or "kg_agent_scheduler.sqlite3",
            workspace_registry_file=os.getenv(
                "KG_AGENT_WORKSPACE_REGISTRY_FILE",
                "kg_agent_workspaces.json",
            ).strip()
            or "kg_agent_workspaces.json",
            workspace_registry_sqlite_path=os.getenv(
                "KG_AGENT_WORKSPACE_REGISTRY_SQLITE_PATH",
                "kg_agent_workspaces.sqlite3",
            ).strip()
            or "kg_agent_workspaces.sqlite3",
            uploads_dir=os.getenv(
                "KG_AGENT_UPLOADS_DIR",
                "kg_agent_uploads",
            ).strip()
            or "kg_agent_uploads",
            cross_session_backend=os.getenv(
                "KG_AGENT_CROSS_SESSION_BACKEND", "memory"
            ).strip()
            or "memory",
            cross_session_mongo_collection=os.getenv(
                "KG_AGENT_CROSS_SESSION_MONGO_COLLECTION",
                "kg_agent_cross_session_messages",
            ).strip()
            or "kg_agent_cross_session_messages",
            cross_session_qdrant_collection_prefix=os.getenv(
                "KG_AGENT_CROSS_SESSION_QDRANT_COLLECTION_PREFIX",
                "kg_agent_cross_session",
            ).strip()
            or "kg_agent_cross_session",
            cross_session_min_content_chars=_env_int(
                "KG_AGENT_CROSS_SESSION_MIN_CONTENT_CHARS", 8
            ),
            cross_session_max_content_chars=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_CONTENT_CHARS", 1200
            ),
            cross_session_max_session_refs=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_SESSION_REFS", 12
            ),
            cross_session_enable_consolidation=_env_bool(
                "KG_AGENT_CROSS_SESSION_ENABLE_CONSOLIDATION", True
            ),
            cross_session_consolidation_similarity_threshold=_env_float(
                "KG_AGENT_CROSS_SESSION_CONSOLIDATION_SIMILARITY_THRESHOLD", 0.82
            ),
            cross_session_consolidation_top_k=_env_int(
                "KG_AGENT_CROSS_SESSION_CONSOLIDATION_TOP_K", 3
            ),
            cross_session_max_cluster_snippets=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_CLUSTER_SNIPPETS", 6
            ),
            cross_session_enable_background_maintenance=_env_bool(
                "KG_AGENT_CROSS_SESSION_ENABLE_BACKGROUND_MAINTENANCE", False
            ),
            cross_session_maintenance_interval_seconds=_env_int(
                "KG_AGENT_CROSS_SESSION_MAINTENANCE_INTERVAL_SECONDS", 1800
            ),
            cross_session_maintenance_batch_size=_env_int(
                "KG_AGENT_CROSS_SESSION_MAINTENANCE_BATCH_SIZE", 100
            ),
            cross_session_aging_stale_after_days=_env_float(
                "KG_AGENT_CROSS_SESSION_AGING_STALE_AFTER_DAYS", 14.0
            ),
            cross_session_aging_delete_after_days=_env_float(
                "KG_AGENT_CROSS_SESSION_AGING_DELETE_AFTER_DAYS", 60.0
            ),
            cross_session_aging_keep_min_occurrences=_env_int(
                "KG_AGENT_CROSS_SESSION_AGING_KEEP_MIN_OCCURRENCES", 2
            ),
            cross_session_aging_max_snippets=_env_int(
                "KG_AGENT_CROSS_SESSION_AGING_MAX_SNIPPETS", 3
            ),
        )


@dataclass
class APIConfig:
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    expose_internal_errors: bool = False

    @classmethod
    def from_env(cls) -> "APIConfig":
        return cls(
            cors_origins=_env_csv("KG_AGENT_CORS_ORIGINS", ["*"]),
            expose_internal_errors=_env_bool(
                "KG_AGENT_EXPOSE_INTERNAL_ERRORS",
                False,
            ),
        )


@dataclass
class AgentRuntimeConfig:
    default_workspace: str = ""
    default_domain_schema: str = "economy"
    skills_dir: str = "skills"
    max_iterations: int = 3
    route_judge_prompt_version: str = "v1"
    memory_window_turns: int = 6
    memory_min_recent_turns: int = 2
    memory_max_context_tokens: int = 1200
    debug: bool = False

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        return cls(
            default_workspace=os.getenv(
                "KG_AGENT_DEFAULT_WORKSPACE", os.getenv("WORKSPACE", "")
            ),
            default_domain_schema=(
                os.getenv("KG_AGENT_DEFAULT_DOMAIN_SCHEMA", "economy").strip()
                or "economy"
            ),
            skills_dir=str(
                resolve_project_path(
                    os.getenv("KG_AGENT_SKILLS_DIR", "skills").strip() or "skills"
                )
            ),
            max_iterations=_env_int("KG_AGENT_MAX_ITERATIONS", 3),
            route_judge_prompt_version=(
                os.getenv("KG_AGENT_ROUTE_JUDGE_PROMPT_VERSION", "v1").strip() or "v1"
            ),
            memory_window_turns=_env_int("KG_AGENT_MEMORY_WINDOW_TURNS", 6),
            memory_min_recent_turns=_env_int("KG_AGENT_MEMORY_MIN_RECENT_TURNS", 2),
            memory_max_context_tokens=_env_int(
                "KG_AGENT_MEMORY_MAX_CONTEXT_TOKENS", 1200
            ),
            debug=_env_bool("KG_AGENT_DEBUG", False),
        )


@dataclass
class KGAgentConfig:
    agent_model: AgentModelConfig = field(default_factory=AgentModelConfig)
    utility_model: AgentModelConfig = field(default_factory=AgentModelConfig)
    tool_config: ToolConfig = field(default_factory=ToolConfig)
    api: APIConfig = field(default_factory=APIConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    skill_runtime: SkillRuntimeConfig = field(default_factory=SkillRuntimeConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    runtime: AgentRuntimeConfig = field(default_factory=AgentRuntimeConfig)
    judge_model: AgentModelConfig | None = None
    answer_model: AgentModelConfig | None = None
    path_model: AgentModelConfig | None = None

    @classmethod
    def from_env(cls) -> "KGAgentConfig":
        return cls(
            agent_model=AgentModelConfig.from_env(),
            utility_model=AgentModelConfig.from_utility_env(),
            tool_config=ToolConfig.from_env(),
            api=APIConfig.from_env(),
            mcp=MCPConfig.from_env(),
            skill_runtime=SkillRuntimeConfig.from_env(),
            crawler=CrawlerConfig.from_env(),
            scheduler=SchedulerConfig.from_env(),
            freshness=FreshnessConfig.from_env(),
            persistence=PersistenceConfig.from_env(),
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

    async def _chat_completion_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        client = self._ensure_client()
        request_kwargs: dict[str, Any] = {
            "model": self.config.model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        response = await client.chat.completions.create(**request_kwargs)
        message = response.choices[0].message
        return message.content or ""

    async def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str:
        return await self._chat_completion_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def stream_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> AsyncIterator[str]:
        client = self._ensure_client()
        stream = await client.chat.completions.create(
            model=self.config.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError):
                delta = None
            if isinstance(delta, str) and delta:
                yield delta

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        raw_text = ""
        json_mode_error: Exception | None = None
        try:
            raw_text = await self._chat_completion_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            json_mode_error = exc
            raw_text = await self.complete_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        try:
            return _parse_json_like_object(raw_text)
        except Exception as parse_exc:
            repair_system_prompt = (
                "You repair malformed JSON objects returned by another model. "
                "Return exactly one valid JSON object and nothing else. "
                "Preserve the original field names and values as much as possible."
            )
            repair_user_prompt = (
                "The following model output should have been a JSON object but failed to parse.\n\n"
                f"Parse error:\n{parse_exc}\n\n"
                f"Malformed output:\n{raw_text}\n"
            )
            repair_max_tokens = max(800, min(max_tokens, 2400))
            repair_json_mode_error: Exception | None = None
            try:
                repaired_text = await self._chat_completion_text(
                    system_prompt=repair_system_prompt,
                    user_prompt=repair_user_prompt,
                    temperature=0.0,
                    max_tokens=repair_max_tokens,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                repair_json_mode_error = exc
                repaired_text = await self.complete_text(
                    system_prompt=repair_system_prompt,
                    user_prompt=repair_user_prompt,
                    temperature=0.0,
                    max_tokens=repair_max_tokens,
                )
            try:
                return _parse_json_like_object(repaired_text)
            except Exception as repair_exc:
                raise ValueError(
                    f"LLM JSON parsing failed. "
                    f"{f'JSON mode error: {json_mode_error}. ' if json_mode_error is not None else ''}"
                    f"{f'Repair JSON mode error: {repair_json_mode_error}. ' if repair_json_mode_error is not None else ''}"
                    f"Initial error: {parse_exc}. "
                    f"Repair error: {repair_exc}."
                ) from repair_exc


class FallbackLLMClient:
    """Prefer a lightweight utility client and fall back to the main client when needed."""

    def __init__(
        self,
        *,
        primary: AgentLLMClient | None,
        fallback: AgentLLMClient | None,
        label: str,
    ):
        self.primary = primary
        self.fallback = fallback
        self.label = label
        self._warned_primary_unavailable = False

    def is_available(self) -> bool:
        return bool(
            (self.primary is not None and self.primary.is_available())
            or (self.fallback is not None and self.fallback.is_available())
        )

    def _resolve_client(self) -> AgentLLMClient:
        if self.primary is not None and self.primary.is_available():
            return self.primary
        if self.fallback is not None and self.fallback.is_available():
            if not self._warned_primary_unavailable:
                logger.warning(
                    "Utility model is not configured for %s; falling back to the main agent model.",
                    self.label,
                )
                self._warned_primary_unavailable = True
            return self.fallback
        raise RuntimeError(
            f"No LLM client is available for {self.label}. Configure the utility model or the main agent model."
        )

    async def complete_text(self, **kwargs) -> str:
        client = self._resolve_client()
        return await client.complete_text(**kwargs)

    async def stream_text(self, **kwargs) -> AsyncIterator[str]:
        client = self._resolve_client()
        async for chunk in client.stream_text(**kwargs):
            yield chunk

    async def complete_json(self, **kwargs) -> dict[str, Any]:
        client = self._resolve_client()
        return await client.complete_json(**kwargs)
