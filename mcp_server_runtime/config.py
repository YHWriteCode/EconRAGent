from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kg_agent.config import (
    AgentLLMClient,
    AgentModelConfig,
    FallbackLLMClient,
    SkillRuntimeConfig,
)
from kg_agent.skills.models import SkillRuntimeTarget

SKILLS_ROOT = Path(os.environ.get("MCP_SKILLS_DIR", "/app/skills")).resolve()
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_DIR", "/workspace")).resolve()
RUNS_ROOT = Path(
    os.environ.get("MCP_RUNS_DIR", str(WORKSPACE_ROOT / "runs"))
).resolve()
STATE_ROOT = Path(
    os.environ.get("MCP_STATE_DIR", str(WORKSPACE_ROOT / "state"))
).resolve()
ENVS_ROOT = Path(
    os.environ.get("MCP_ENVS_DIR", str(WORKSPACE_ROOT / "envs"))
).resolve()
OUTPUT_ROOT = (
    Path(os.environ["MCP_OUTPUT_DIR"]).resolve()
    if os.environ.get("MCP_OUTPUT_DIR", "").strip()
    else None
)
WHEELHOUSE_ROOT = Path(
    os.environ.get("MCP_WHEELHOUSE_DIR", str(WORKSPACE_ROOT / "wheelhouse"))
).resolve()
PIP_CACHE_ROOT = Path(
    os.environ.get("MCP_PIP_CACHE_DIR", str(WORKSPACE_ROOT / "pip-cache"))
).resolve()
LOCKS_ROOT = Path(
    os.environ.get("MCP_LOCKS_DIR", str(WORKSPACE_ROOT / "locks"))
).resolve()
RUN_STORE_DB_PATH = Path(
    os.environ.get(
        "MCP_RUN_STORE_SQLITE_PATH",
        str(STATE_ROOT / "skill_runtime_runs.sqlite3"),
    )
).resolve()
DEFAULT_SCRIPT_TIMEOUT_S = int(os.environ.get("MCP_SCRIPT_TIMEOUT_S", "120"))
DEFAULT_RUN_TIMEOUT_S = int(os.environ.get("MCP_RUN_TIMEOUT_S", "300"))
MAX_REFERENCE_BYTES = int(os.environ.get("MCP_MAX_REFERENCE_BYTES", "200000"))
MAX_LOG_PREVIEW_BYTES = int(os.environ.get("MCP_MAX_LOG_PREVIEW_BYTES", "12000"))
MAX_GENERATED_FILE_BYTES = 64000
MAX_TRANSPORT_GENERATED_FILE_PREVIEW_BYTES = int(
    os.environ.get("MCP_MAX_TRANSPORT_GENERATED_FILE_PREVIEW_BYTES", "4096")
)
MAX_TRANSPORT_GENERATED_FILES_TOTAL_BYTES = int(
    os.environ.get("MCP_MAX_TRANSPORT_GENERATED_FILES_TOTAL_BYTES", "12288")
)
MAX_REPAIR_ATTEMPTS = max(
    1,
    int(os.environ.get("MCP_FREE_SHELL_MAX_REPAIR_ATTEMPTS", "3")),
)
MAX_BOOTSTRAP_ATTEMPTS = max(
    1,
    int(os.environ.get("MCP_FREE_SHELL_MAX_BOOTSTRAP_ATTEMPTS", "2")),
)
WAIT_FOR_TERMINAL_GRACE_S = float(
    os.environ.get("MCP_WAIT_FOR_TERMINAL_GRACE_S", "10.0")
)
WORKER_TERMINAL_POLL_INTERVAL_S = float(
    os.environ.get("MCP_WORKER_TERMINAL_POLL_INTERVAL_S", "0.2")
)
WORKER_HEARTBEAT_TIMEOUT_S = float(
    os.environ.get("MCP_WORKER_HEARTBEAT_TIMEOUT_S", "30.0")
)
QUEUE_WORKER_POLL_INTERVAL_S = float(
    os.environ.get("MCP_QUEUE_WORKER_POLL_INTERVAL_S", "0.5")
)
QUEUE_WORKER_STARTUP_WAIT_S = float(
    os.environ.get("MCP_QUEUE_WORKER_STARTUP_WAIT_S", "1.5")
)
QUEUE_WORKER_CONCURRENCY = max(
    1,
    int(os.environ.get("MCP_QUEUE_WORKER_CONCURRENCY", "1")),
)
QUEUE_LEASE_TIMEOUT_S = float(
    os.environ.get("MCP_QUEUE_LEASE_TIMEOUT_S", "45.0")
)
QUEUE_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("MCP_QUEUE_MAX_ATTEMPTS", "2")),
)
ENV_HASH_FORMAT_VERSION = "skill-env-v1"
ENV_BUILD_TIMEOUT_S = max(
    30,
    int(os.environ.get("MCP_ENV_BUILD_TIMEOUT_S", "600")),
)
ENV_LOCK_TIMEOUT_S = max(
    5.0,
    float(os.environ.get("MCP_ENV_LOCK_TIMEOUT_S", "300.0")),
)
ENV_LOCK_STALE_S = max(
    ENV_LOCK_TIMEOUT_S,
    float(os.environ.get("MCP_ENV_LOCK_STALE_S", "1800.0")),
)
ENV_LOCK_POLL_INTERVAL_S = max(
    0.05,
    float(os.environ.get("MCP_ENV_LOCK_POLL_INTERVAL_S", "0.2")),
)

SKILL_RUNTIME_CONFIG = SkillRuntimeConfig.from_env()
DEFAULT_RUNTIME_TARGET = SkillRuntimeTarget.from_dict(
    SKILL_RUNTIME_CONFIG.default_runtime_target.to_dict(),
    default=SkillRuntimeTarget.linux_default(),
)


def _build_utility_llm_client() -> FallbackLLMClient:
    utility_config = AgentModelConfig.from_env_keys(
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
    primary_client = (
        AgentLLMClient(utility_config) if utility_config.is_configured() else None
    )
    fallback_config = AgentModelConfig.from_env()
    fallback_client = (
        AgentLLMClient(fallback_config) if fallback_config.is_configured() else None
    )
    return FallbackLLMClient(
        primary=primary_client,
        fallback=fallback_client,
        label="skill runtime free-shell planning",
    )


UTILITY_LLM_CLIENT = _build_utility_llm_client()


class _SerializedUtilityLLMStub:
    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = [dict(item) for item in payloads]

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        if not self.payloads:
            raise RuntimeError("No serialized utility LLM payload remaining")
        return self.payloads.pop(0)
