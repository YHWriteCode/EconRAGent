from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from kg_agent.skills.models import LoadedSkill, SkillCommandPlan, SkillRuntimeTarget
from kg_agent.skills.command_planner import NODE_RUNTIME_SUFFIXES

from .config import (
    ENVS_ROOT,
    ENV_BUILD_TIMEOUT_S,
    ENV_HASH_FORMAT_VERSION,
    ENV_LOCK_POLL_INTERVAL_S,
    ENV_LOCK_STALE_S,
    ENV_LOCK_TIMEOUT_S,
    LOCKS_ROOT,
    OUTPUT_ROOT,
    PIP_CACHE_ROOT,
    WHEELHOUSE_ROOT,
)
from .errors import SkillServerError
from .skills import _build_loaded_skill_from_dir, _iter_runnable_scripts
from .utils import shell_join, utc_now
from .workspace import _write_json_atomic


def _runtime_bin_dir_name() -> str:
    return "Scripts" if os.name == "nt" else "bin"


def _runtime_python_name() -> str:
    return "python.exe" if os.name == "nt" else "python"


def _venv_bin_dir(env_dir: Path) -> Path:
    return (env_dir / _runtime_bin_dir_name()).resolve()


def _venv_python_path(env_dir: Path) -> Path:
    # Keep the venv-local interpreter path instead of resolving its symlink target.
    return _venv_bin_dir(env_dir) / _runtime_python_name()


def _skill_env_metadata_path(env_dir: Path) -> Path:
    return (env_dir / "env_metadata.json").resolve()


def _skill_env_lock_path(env_hash: str) -> Path:
    return (LOCKS_ROOT / f"{env_hash}.lock").resolve()


def _normalize_skill_metadata_paths(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()
        for item in value
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]


def _resolve_skill_dependency_file(loaded_skill: LoadedSkill) -> Path | None:
    skill_dir = loaded_skill.skill.path.resolve()
    metadata = (
        loaded_skill.skill.metadata
        if isinstance(loaded_skill.skill.metadata, dict)
        else {}
    )
    candidates = [
        *_normalize_skill_metadata_paths(metadata.get("runtime_requirements")),
        *_normalize_skill_metadata_paths(metadata.get("requirements")),
        "requirements.lock",
        "requirements.txt",
    ]
    seen: set[str] = set()
    for raw_relative in candidates:
        relative_path = str(raw_relative).replace("\\", "/").strip()
        if not relative_path or relative_path in seen:
            continue
        seen.add(relative_path)
        candidate = (skill_dir / relative_path).resolve()
        if candidate == skill_dir or skill_dir not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


def _build_skill_env_spec(loaded_skill: LoadedSkill) -> dict[str, Any] | None:
    dependency_file = _resolve_skill_dependency_file(loaded_skill)
    if dependency_file is None:
        return None
    raw_bytes = dependency_file.read_bytes()
    requirements_hash = hashlib.sha256(raw_bytes).hexdigest()
    env_hash = hashlib.sha256(
        "\n".join(
            [
                ENV_HASH_FORMAT_VERSION,
                platform.system().lower(),
                platform.machine().lower(),
                f"{sys.version_info.major}.{sys.version_info.minor}",
                requirements_hash,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    env_name = f"py{sys.version_info.major}{sys.version_info.minor}-{env_hash}"
    env_dir = (ENVS_ROOT / env_name).resolve()
    return {
        "enabled": True,
        "strategy": "shared_wheelhouse_venv",
        "env_hash": env_hash,
        "env_name": env_name,
        "env_path": str(env_dir),
        "bin_dir": str(_venv_bin_dir(env_dir)),
        "python_path": str(_venv_python_path(env_dir)),
        "dependency_file": str(
            dependency_file.relative_to(loaded_skill.skill.path.resolve())
        ).replace("\\", "/"),
        "dependency_path": str(dependency_file),
        "dependency_hash": requirements_hash,
        "wheelhouse_root": str(WHEELHOUSE_ROOT),
        "pip_cache_dir": str(PIP_CACHE_ROOT),
        "ready": False,
        "materialized": False,
        "reused": False,
        "requires_materialization": False,
        "error": None,
    }


def _load_skill_env_metadata(env_dir: Path) -> dict[str, Any] | None:
    metadata_path = _skill_env_metadata_path(env_dir)
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _skill_env_is_ready(env_spec: dict[str, Any]) -> bool:
    env_dir = Path(str(env_spec.get("env_path", ""))).resolve()
    python_path = Path(str(env_spec.get("python_path", "")))
    metadata = _load_skill_env_metadata(env_dir)
    if metadata is None:
        return False
    if str(metadata.get("env_hash", "")).strip() != str(env_spec.get("env_hash", "")).strip():
        return False
    if (
        str(metadata.get("dependency_hash", "")).strip()
        != str(env_spec.get("dependency_hash", "")).strip()
    ):
        return False
    if str(metadata.get("format_version", "")).strip() != ENV_HASH_FORMAT_VERSION:
        return False
    if not (env_dir.is_dir() and python_path.is_file()):
        return False
    dependency_path = Path(str(env_spec.get("dependency_path", ""))).resolve()
    if dependency_path.is_file() and not _skill_env_has_required_distributions(
        python_path=python_path,
        dependency_path=dependency_path,
    ):
        return False
    return True


def _skill_env_runtime_payload(env_spec: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(env_spec, dict):
        return {
            "enabled": False,
            "strategy": "shared_wheelhouse_venv",
        }
    payload = {
        "enabled": bool(env_spec.get("enabled", False)),
        "strategy": str(env_spec.get("strategy", "shared_wheelhouse_venv")).strip()
        or "shared_wheelhouse_venv",
        "env_hash": str(env_spec.get("env_hash", "")).strip(),
        "env_name": str(env_spec.get("env_name", "")).strip(),
        "env_path": str(env_spec.get("env_path", "")).strip(),
        "python_path": str(env_spec.get("python_path", "")).strip(),
        "dependency_file": str(env_spec.get("dependency_file", "")).strip(),
        "dependency_hash": str(env_spec.get("dependency_hash", "")).strip(),
        "wheelhouse_root": str(env_spec.get("wheelhouse_root", "")).strip(),
        "pip_cache_dir": str(env_spec.get("pip_cache_dir", "")).strip(),
        "ready": bool(env_spec.get("ready", False)),
        "materialized": bool(env_spec.get("materialized", False)),
        "reused": bool(env_spec.get("reused", False)),
        "requires_materialization": bool(
            env_spec.get("requires_materialization", False)
        ),
    }
    error_text = str(env_spec.get("error", "")).strip()
    if error_text:
        payload["error"] = error_text
    return payload


def _command_references_skill_script(
    command_plan: SkillCommandPlan,
    loaded_skill: LoadedSkill,
) -> bool:
    command = str(command_plan.command or "").replace("\\", "/").strip()
    if not command:
        return False
    for relative_path in _iter_runnable_scripts(loaded_skill.skill.path.resolve()):
        normalized = relative_path.replace("\\", "/")
        if normalized and normalized in command:
            return True
    return False


def _command_plan_prefers_skill_env(
    command_plan: SkillCommandPlan,
    loaded_skill: LoadedSkill,
) -> bool:
    if command_plan.entrypoint and not str(command_plan.entrypoint).replace("\\", "/").startswith(
        ".skill_generated/"
    ):
        return True
    if command_plan.mode in {"inferred", "declared_example", "structured_args"}:
        return True
    return _command_references_skill_script(command_plan, loaded_skill)


def _acquire_file_lock(lock_path: Path) -> int | None:
    try:
        return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def _wait_for_skill_env_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    while True:
        handle = _acquire_file_lock(lock_path)
        if handle is not None:
            payload = {
                "pid": os.getpid(),
                "created_at": utc_now(),
            }
            os.write(handle, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return handle
        if lock_path.exists():
            lock_age_s = max(0.0, time.time() - lock_path.stat().st_mtime)
            if lock_age_s > ENV_LOCK_STALE_S:
                lock_path.unlink(missing_ok=True)
                continue
        if (time.monotonic() - started_at) >= ENV_LOCK_TIMEOUT_S:
            raise SkillServerError(
                f"Timed out waiting for skill environment lock: {lock_path.name}"
            )
        time.sleep(ENV_LOCK_POLL_INTERVAL_S)


def _release_skill_env_lock(lock_path: Path, handle: int | None) -> None:
    if handle is not None:
        try:
            os.close(handle)
        except OSError:
            pass
    lock_path.unlink(missing_ok=True)


def _build_pip_process_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PIP_CACHE_DIR"] = str(PIP_CACHE_ROOT)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _run_subprocess_checked(
    argv: list[str],
    *,
    timeout_s: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=_build_pip_process_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _try_seed_wheelhouse_from_dependency_file(dependency_path: Path) -> None:
    try:
        result = _run_subprocess_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(WHEELHOUSE_ROOT),
                "-r",
                str(dependency_path),
            ],
            timeout_s=ENV_BUILD_TIMEOUT_S,
        )
    except Exception:
        return
    if result.returncode != 0:
        return


def _dependency_distribution_names(dependency_path: Path) -> list[str]:
    try:
        lines = dependency_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        candidate = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip()
        if not candidate:
            continue
        normalized = candidate.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        names.append(candidate)
    return names


def _skill_env_has_required_distributions(
    *,
    python_path: Path,
    dependency_path: Path,
) -> bool:
    distributions = _dependency_distribution_names(dependency_path)
    if not distributions:
        return True
    check_result = _run_subprocess_checked(
        [
            str(python_path),
            "-m",
            "pip",
            "show",
            *distributions,
        ],
        timeout_s=ENV_BUILD_TIMEOUT_S,
    )
    return check_result.returncode == 0


def _ensure_skill_env_ready(
    env_spec: dict[str, Any] | None,
    *,
    allow_network: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(env_spec, dict):
        return None
    resolved = dict(env_spec)
    env_dir = Path(str(resolved["env_path"])).resolve()
    resolved["ready"] = _skill_env_is_ready(resolved)
    if resolved["ready"]:
        resolved["materialized"] = True
        resolved["reused"] = True
        return resolved

    dependency_path = Path(str(resolved["dependency_path"])).resolve()
    lock_path = _skill_env_lock_path(str(resolved["env_hash"]))
    temp_env_dir = (
        ENVS_ROOT / f"{str(resolved['env_name']).strip()}.tmp-{uuid.uuid4().hex}"
    ).resolve()
    lock_handle: int | None = None
    try:
        lock_handle = _wait_for_skill_env_lock(lock_path)
        resolved["ready"] = _skill_env_is_ready(resolved)
        if resolved["ready"]:
            resolved["materialized"] = True
            resolved["reused"] = True
            return resolved

        temp_env_dir.parent.mkdir(parents=True, exist_ok=True)
        WHEELHOUSE_ROOT.mkdir(parents=True, exist_ok=True)
        PIP_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        create_result = _run_subprocess_checked(
            [sys.executable, "-m", "venv", str(temp_env_dir)],
            timeout_s=ENV_BUILD_TIMEOUT_S,
        )
        if create_result.returncode != 0:
            raise SkillServerError(
                "Failed to create isolated skill environment: "
                + (create_result.stderr.strip() or create_result.stdout.strip() or "venv failed")
            )

        offline_install_result = _run_subprocess_checked(
            [
                str(_venv_python_path(temp_env_dir)),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(WHEELHOUSE_ROOT),
                "-r",
                str(dependency_path),
            ],
            timeout_s=ENV_BUILD_TIMEOUT_S,
        )
        install_result = offline_install_result
        install_mode = "offline_wheelhouse"
        if install_result.returncode != 0 and allow_network:
            install_result = _run_subprocess_checked(
                [
                    str(_venv_python_path(temp_env_dir)),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(dependency_path),
                ],
                timeout_s=ENV_BUILD_TIMEOUT_S,
            )
            install_mode = "online_index"
            if install_result.returncode == 0:
                _try_seed_wheelhouse_from_dependency_file(dependency_path)
        if install_result.returncode != 0:
            error_details = (
                install_result.stderr.strip()
                or install_result.stdout.strip()
                or "pip install failed"
            )
            if allow_network:
                offline_details = (
                    offline_install_result.stderr.strip()
                    or offline_install_result.stdout.strip()
                    or "offline wheelhouse install failed"
                )
                raise SkillServerError(
                    "Failed to install isolated skill dependencies. "
                    f"dependency_file={resolved['dependency_file']} "
                    f"wheelhouse_root={WHEELHOUSE_ROOT}. "
                    f"Offline wheelhouse install failed first: {offline_details}. "
                    f"Online fallback ({install_mode}) also failed: {error_details}"
                )
            raise SkillServerError(
                "Failed to install isolated skill dependencies from wheelhouse. "
                f"dependency_file={resolved['dependency_file']} "
                f"wheelhouse_root={WHEELHOUSE_ROOT}. "
                + error_details
            )

        metadata = {
            "format_version": ENV_HASH_FORMAT_VERSION,
            "env_hash": str(resolved["env_hash"]),
            "env_name": str(resolved["env_name"]),
            "dependency_file": str(resolved["dependency_file"]),
            "dependency_hash": str(resolved["dependency_hash"]),
            "created_at": utc_now(),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform": platform.system().lower(),
            "machine": platform.machine().lower(),
        }
        _write_json_atomic(_skill_env_metadata_path(temp_env_dir), metadata)
        if env_dir.exists():
            shutil.rmtree(env_dir, ignore_errors=True)
        temp_env_dir.replace(env_dir)
        resolved["bin_dir"] = str(_venv_bin_dir(env_dir))
        resolved["python_path"] = str(_venv_python_path(env_dir))
        resolved["ready"] = True
        resolved["materialized"] = True
        resolved["reused"] = False
        return resolved
    finally:
        shutil.rmtree(temp_env_dir, ignore_errors=True)
        _release_skill_env_lock(lock_path, lock_handle)


def _prefetch_skill_wheels_for_skill(loaded_skill: LoadedSkill) -> dict[str, Any]:
    env_spec = _build_skill_env_spec(loaded_skill)
    if env_spec is None:
        return {
            "skill_name": loaded_skill.skill.name,
            "dependency_file": None,
            "downloaded": False,
            "skipped": True,
            "reason": "no_dependency_file",
        }
    WHEELHOUSE_ROOT.mkdir(parents=True, exist_ok=True)
    PIP_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    dependency_path = Path(str(env_spec["dependency_path"])).resolve()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(WHEELHOUSE_ROOT),
            "-r",
            str(dependency_path),
        ],
        env=_build_pip_process_env(),
        capture_output=True,
        text=True,
        timeout=ENV_BUILD_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise SkillServerError(
            f"Failed to prefetch wheels for skill '{loaded_skill.skill.name}': "
            + (result.stderr.strip() or result.stdout.strip() or "pip download failed")
        )
    return {
        "skill_name": loaded_skill.skill.name,
        "dependency_file": str(env_spec["dependency_file"]),
        "downloaded": True,
        "wheelhouse_root": str(WHEELHOUSE_ROOT),
        "pip_cache_dir": str(PIP_CACHE_ROOT),
        "env_hash": str(env_spec["env_hash"]),
    }


def _default_shell_command(relative_script: str) -> str:
    suffix = Path(relative_script).suffix.lower()
    quoted_script = shlex.quote(relative_script)
    if suffix == ".py":
        return f"python {quoted_script}"
    if suffix in {".sh", ".bash"}:
        return f"/bin/sh {quoted_script}"
    if suffix == ".ps1":
        return f"powershell -File {quoted_script}"
    return quoted_script


def _bootstrap_workspace_paths(workspace_dir: Path) -> dict[str, Path]:
    root = (workspace_dir / ".skill_bootstrap").resolve()
    return {
        "root": root,
        "bin": (root / "bin").resolve(),
        "scripts": (root / "Scripts").resolve(),
        "site_packages": (root / "site-packages").resolve(),
    }


def _bootstrap_cache_key(loaded_skill: LoadedSkill) -> str:
    payload = "\n".join(
        [
            loaded_skill.skill.name,
            str(loaded_skill.skill.path.resolve()),
            loaded_skill.skill_md,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _normalize_bootstrap_skill_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip().lower())
    normalized = normalized.strip("-._")
    return normalized or "skill"


def _shared_bootstrap_paths(loaded_skill: LoadedSkill) -> dict[str, Path]:
    skill_name = _normalize_bootstrap_skill_name(loaded_skill.skill.name)
    cache_key = _bootstrap_cache_key(loaded_skill)
    root = (ENVS_ROOT / "bootstrap" / f"{skill_name}-{cache_key}").resolve()
    return {
        "root": root,
        "bin": (root / "bin").resolve(),
        "scripts": (root / "Scripts").resolve(),
        "site_packages": (root / "site-packages").resolve(),
    }


def _detect_bootstrap_node_runtime_bin(bootstrap_root: Path) -> Path | None:
    runtime_root = (bootstrap_root / ".node-runtime").resolve()
    if not runtime_root.is_dir():
        return None
    install_roots = sorted(
        (item for item in runtime_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
        reverse=True,
    )
    for install_root in install_roots:
        for candidate in ((install_root / "bin").resolve(), install_root.resolve()):
            node_candidates = [candidate / "node", candidate / "node.exe"]
            npm_candidates = [candidate / "npm", candidate / "npm.cmd", candidate / "npm.exe"]
            if any(path.is_file() for path in node_candidates) and any(
                path.is_file() for path in npm_candidates
            ):
                return candidate
    return None


def _bootstrap_node_module_paths(bootstrap_root: Path) -> list[Path]:
    candidates = [
        (bootstrap_root / "lib" / "node_modules").resolve(),
        (bootstrap_root / "node_modules").resolve(),
    ]
    return [path for path in candidates if path.is_dir()]


def _resolve_bootstrap_paths(
    *,
    workspace_dir: Path,
    loaded_skill: LoadedSkill | None,
    command_plan: SkillCommandPlan | None,
) -> tuple[dict[str, Path], bool]:
    use_shared_bootstrap = bool(
        loaded_skill is not None
        and isinstance(command_plan, SkillCommandPlan)
        and command_plan.shell_mode == "free_shell"
    )
    if use_shared_bootstrap and loaded_skill is not None:
        return _shared_bootstrap_paths(loaded_skill), True
    return _bootstrap_workspace_paths(workspace_dir), False


def _build_script_shell_command(
    *,
    skill_dir: Path,
    relative_script: str,
    cli_args: list[str] | None = None,
) -> str:
    script_path = (skill_dir / relative_script).resolve()
    suffix = script_path.suffix.lower()
    argv: list[str]
    if suffix == ".py":
        argv = ["python", str(script_path)]
    elif suffix in NODE_RUNTIME_SUFFIXES:
        argv = ["node", str(script_path)]
    elif suffix in {".sh", ".bash"}:
        shell_bin = "sh.exe" if os.name == "nt" else "/bin/sh"
        argv = [shell_bin, str(script_path)]
    elif suffix == ".ps1":
        shell_bin = "powershell.exe" if os.name == "nt" else "pwsh"
        argv = [shell_bin]
        if os.name == "nt":
            argv.append("-NoProfile")
        argv.extend(["-File", str(script_path)])
    else:
        argv = [str(script_path)]
    argv.extend(str(item) for item in (cli_args or []))
    return shell_join(argv)


def _rewrite_runtime_workspace_path_text(
    *,
    value: str | None,
    runtime_target: SkillRuntimeTarget | None,
    workspace_dir: Path | None,
) -> str | None:
    if value is None or workspace_dir is None or runtime_target is None:
        return value
    rewritten = str(value)
    raw_workspace_root = str(runtime_target.workspace_root or "").strip()
    if not raw_workspace_root:
        return rewritten

    workspace_text = str(workspace_dir)
    workspace_posix = workspace_text.replace("\\", "/")
    normalized_workspace_root = raw_workspace_root.rstrip("/\\")
    output_placeholders: list[tuple[str, str]] = []

    def _register_output_placeholder(candidate_root: str, replacement_root: str) -> None:
        normalized_candidate = str(candidate_root or "").rstrip("/\\")
        normalized_replacement = str(replacement_root or "").rstrip("/\\")
        if not normalized_candidate or not normalized_replacement:
            return
        placeholder = f"__MCP_OUTPUT_ROOT_{len(output_placeholders)}__"
        output_placeholders.append((placeholder, normalized_replacement))
        rewritten_roots = (
            (normalized_candidate + "/", placeholder + "/"),
            (normalized_candidate + "\\", placeholder + "\\"),
            (normalized_candidate, placeholder),
        )
        nonlocal rewritten
        for candidate, replacement in rewritten_roots:
            rewritten = rewritten.replace(candidate, replacement)

    if OUTPUT_ROOT is not None and normalized_workspace_root:
        shared_output_dir = (OUTPUT_ROOT / workspace_dir.name).resolve()
        shared_output_text = str(shared_output_dir)
        shared_output_posix = shared_output_text.replace("\\", "/")
        _register_output_placeholder(
            f"{normalized_workspace_root}/output",
            shared_output_posix,
        )
        _register_output_placeholder(
            f"{normalized_workspace_root}\\output",
            shared_output_text,
        )

    for candidate_root, replacement_root in (
        (raw_workspace_root, workspace_text),
        (raw_workspace_root.replace("\\", "/"), workspace_posix),
    ):
        normalized_candidate = str(candidate_root or "").rstrip("/\\")
        normalized_replacement = str(replacement_root or "").rstrip("/\\")
        if not normalized_candidate or not normalized_replacement:
            continue
        rewritten = rewritten.replace(normalized_candidate + "/", normalized_replacement + "/")
        rewritten = rewritten.replace(normalized_candidate + "\\", normalized_replacement + "\\")
        rewritten = rewritten.replace(normalized_candidate, normalized_replacement)

    for placeholder, replacement_root in output_placeholders:
        rewritten = rewritten.replace(placeholder + "/", replacement_root + "/")
        rewritten = rewritten.replace(placeholder + "\\", replacement_root + "\\")
        rewritten = rewritten.replace(placeholder, replacement_root)
    return rewritten


def _rewrite_cli_args_for_workspace(
    *,
    cli_args: list[str] | None,
    runtime_target: SkillRuntimeTarget | None,
    workspace_dir: Path | None,
) -> list[str]:
    rewritten_args: list[str] = []
    for item in cli_args or []:
        rewritten = _rewrite_runtime_workspace_path_text(
            value=str(item),
            runtime_target=runtime_target,
            workspace_dir=workspace_dir,
        )
        rewritten_args.append(str(rewritten if rewritten is not None else item))
    return rewritten_args


def _build_workspace_script_shell_command(
    *,
    workspace_dir: Path,
    relative_script: str,
    cli_args: list[str] | None = None,
    runtime_target: SkillRuntimeTarget | None = None,
) -> str:
    script_path = (workspace_dir / relative_script).resolve()
    suffix = script_path.suffix.lower()
    argv: list[str]
    if suffix == ".py":
        argv = ["python", str(script_path)]
    elif suffix in NODE_RUNTIME_SUFFIXES:
        argv = ["node", str(script_path)]
    elif suffix in {".sh", ".bash"}:
        shell_bin = "sh.exe" if os.name == "nt" else "/bin/sh"
        argv = [shell_bin, str(script_path)]
    elif suffix == ".ps1":
        shell_bin = "powershell.exe" if os.name == "nt" else "pwsh"
        argv = [shell_bin]
        if os.name == "nt":
            argv.append("-NoProfile")
        argv.extend(["-File", str(script_path)])
    else:
        argv = [str(script_path)]
    argv.extend(
        _rewrite_cli_args_for_workspace(
            cli_args=cli_args,
            runtime_target=runtime_target,
            workspace_dir=workspace_dir,
        )
    )
    return shell_join(argv)


def _rewrite_command_for_workspace(
    *,
    command: str | None,
    loaded_skill: LoadedSkill,
    workspace_dir: Path | None,
    runtime_target: SkillRuntimeTarget | None = None,
) -> str | None:
    if command is None or workspace_dir is None:
        return command
    rewritten = str(command)
    skill_dir = loaded_skill.skill.path.resolve()
    replacements = [
        str(skill_dir),
        str(skill_dir).replace("\\", "/"),
    ]
    workspace_text = str(workspace_dir)
    workspace_posix = workspace_text.replace("\\", "/")
    for candidate in replacements:
        if candidate:
            rewritten = rewritten.replace(candidate, workspace_text)
            rewritten = rewritten.replace(candidate.replace("\\", "/"), workspace_posix)
    return _rewrite_runtime_workspace_path_text(
        value=rewritten,
        runtime_target=runtime_target,
        workspace_dir=workspace_dir,
    )


def _build_run_env(
    *,
    skill_dir: Path,
    workspace_dir: Path,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
    runtime_target: SkillRuntimeTarget,
    request_file: Path,
    context_file: Path | None = None,
    loaded_skill: LoadedSkill | None = None,
    command_plan: SkillCommandPlan | None = None,
    materialize_skill_env: bool = False,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    env = os.environ.copy()
    bootstrap_paths, shared_bootstrap = _resolve_bootstrap_paths(
        workspace_dir=workspace_dir,
        loaded_skill=loaded_skill,
        command_plan=command_plan,
    )
    for path in bootstrap_paths.values():
        path.mkdir(parents=True, exist_ok=True)
    skill_env_spec = (
        _build_skill_env_spec(loaded_skill) if isinstance(loaded_skill, LoadedSkill) else None
    )
    use_skill_env = bool(
        skill_env_spec
        and isinstance(command_plan, SkillCommandPlan)
        and _command_plan_prefers_skill_env(command_plan, loaded_skill)
    )
    if skill_env_spec is not None:
        skill_env_spec["requires_materialization"] = use_skill_env
        skill_env_spec["ready"] = _skill_env_is_ready(skill_env_spec)
        skill_env_spec["materialized"] = bool(skill_env_spec["ready"])
        skill_env_spec["reused"] = bool(skill_env_spec["ready"])
        if use_skill_env and materialize_skill_env:
            skill_env_spec = _ensure_skill_env_ready(
                skill_env_spec,
                allow_network=bool(runtime_target.network_allowed),
            )

    bootstrap_node_bin = _detect_bootstrap_node_runtime_bin(bootstrap_paths["root"])
    python_bin_dir = str(Path(sys.executable).resolve().parent)
    active_python_bin_dir = (
        str(skill_env_spec["bin_dir"])
        if skill_env_spec is not None and bool(skill_env_spec.get("ready"))
        else python_bin_dir
    )
    existing_path = env.get("PATH", "")
    path_entries = [
        active_python_bin_dir,
        str(bootstrap_node_bin) if bootstrap_node_bin is not None else "",
        str(bootstrap_paths["bin"]),
        str(bootstrap_paths["scripts"]),
        python_bin_dir,
    ]
    if existing_path:
        path_entries.append(existing_path)
    env["PATH"] = os.pathsep.join(
        entry for entry in path_entries if isinstance(entry, str) and entry
    )
    existing_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_entries = [str(bootstrap_paths["site_packages"])]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(
        entry for entry in pythonpath_entries if isinstance(entry, str) and entry
    )
    existing_node_path = env.get("NODE_PATH", "")
    node_path_entries = [
        str(path) for path in _bootstrap_node_module_paths(bootstrap_paths["root"])
    ]
    if existing_node_path:
        node_path_entries.append(existing_node_path)
    if node_path_entries:
        env["NODE_PATH"] = os.pathsep.join(
            entry for entry in node_path_entries if isinstance(entry, str) and entry
        )
    env["PIP_CACHE_DIR"] = str(PIP_CACHE_ROOT)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_FIND_LINKS"] = str(WHEELHOUSE_ROOT)
    env["NPM_CONFIG_PREFIX"] = str(bootstrap_paths["root"])
    env["npm_config_prefix"] = str(bootstrap_paths["root"])
    npm_cache_dir = (bootstrap_paths["root"] / ".npm-cache").resolve()
    npm_cache_dir.mkdir(parents=True, exist_ok=True)
    env["NPM_CONFIG_CACHE"] = str(npm_cache_dir)
    env["npm_config_cache"] = str(npm_cache_dir)
    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    env["npm_config_update_notifier"] = "false"
    if skill_env_spec is not None and bool(skill_env_spec.get("ready")):
        env["VIRTUAL_ENV"] = str(skill_env_spec["env_path"])
        env["SKILL_VENV_BIN"] = str(skill_env_spec["bin_dir"])
        env["SKILL_PYTHON_BIN"] = str(skill_env_spec["python_path"])
    else:
        env["SKILL_PYTHON_BIN"] = str(Path(sys.executable).resolve())
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "SKILL_NAME": skill_dir.name,
            "SKILL_ROOT": str(skill_dir),
            "SKILL_WORKSPACE": str(workspace_dir),
            "SKILL_GOAL": goal,
            "SKILL_USER_QUERY": user_query,
            "SKILL_CONSTRAINTS_JSON": json.dumps(constraints, ensure_ascii=False),
            "SKILL_REQUEST_FILE": str(request_file),
            "SKILL_INVOCATION_FILE": str(request_file),
            "SKILL_CONTEXT_FILE": str(context_file or ""),
            "SKILL_NETWORK_ALLOWED": "true" if runtime_target.network_allowed else "false",
            "SKILL_BOOTSTRAP_ROOT": str(bootstrap_paths["root"]),
            "SKILL_BOOTSTRAP_BIN": str(bootstrap_paths["bin"]),
            "SKILL_BOOTSTRAP_SCRIPTS": str(bootstrap_paths["scripts"]),
            "SKILL_BOOTSTRAP_SITE_PACKAGES": str(bootstrap_paths["site_packages"]),
            "SKILL_BOOTSTRAP_SHARED": "true" if shared_bootstrap else "false",
            "PIP_TARGET": str(bootstrap_paths["site_packages"]),
            "HOME": str(workspace_dir),
        }
    )
    if skill_env_spec is not None:
        env["SKILL_ENVIRONMENT_JSON"] = json.dumps(
            _skill_env_runtime_payload(skill_env_spec),
            ensure_ascii=False,
        )
    return env, skill_env_spec


def _build_script_env(
    skill_dir: Path,
    workspace_dir: Path,
    *,
    relative_script: str,
    default_runtime_target: SkillRuntimeTarget,
) -> dict[str, str]:
    loaded_skill = _build_loaded_skill_from_dir(skill_dir)
    dummy_plan = SkillCommandPlan(
        skill_name=loaded_skill.skill.name,
        goal="legacy execute_skill_script",
        user_query="legacy execute_skill_script",
        runtime_target=default_runtime_target,
        entrypoint=relative_script,
        mode="inferred",
    )
    env, _skill_env_spec = _build_run_env(
        skill_dir=skill_dir,
        workspace_dir=workspace_dir,
        goal="legacy execute_skill_script",
        user_query="legacy execute_skill_script",
        constraints={},
        runtime_target=default_runtime_target,
        request_file=workspace_dir / "skill_request.json",
        loaded_skill=loaded_skill,
        command_plan=dummy_plan,
        materialize_skill_env=True,
    )
    return env
