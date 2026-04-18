from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence


@dataclass
class RuntimeCliDeps:
    workspace_root: Path
    runs_root: Path
    state_root: Path
    envs_root: Path
    wheelhouse_root: Path
    pip_cache_root: Path
    locks_root: Path
    ensure_run_store_initialized: Callable[[], None]
    prefetch_skill_wheels_cli: Callable[..., dict[str, Any]]
    run_queue_worker_loop: Callable[[], Awaitable[int]]
    run_durable_worker: Callable[[str], Awaitable[int]]
    ensure_queue_worker_processes: Callable[[], list[int]]
    mcp: Any


def run_main(argv: Sequence[str] | None = None, *, deps: RuntimeCliDeps) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-run-id", default="")
    parser.add_argument("--queue-worker", action="store_true")
    parser.add_argument("--prefetch-skill-wheels", default="")
    parser.add_argument("--prefetch-all-skill-wheels", action="store_true")
    args, _unknown = parser.parse_known_args(argv)

    deps.workspace_root.mkdir(parents=True, exist_ok=True)
    deps.runs_root.mkdir(parents=True, exist_ok=True)
    deps.state_root.mkdir(parents=True, exist_ok=True)
    deps.envs_root.mkdir(parents=True, exist_ok=True)
    deps.wheelhouse_root.mkdir(parents=True, exist_ok=True)
    deps.pip_cache_root.mkdir(parents=True, exist_ok=True)
    deps.locks_root.mkdir(parents=True, exist_ok=True)
    deps.ensure_run_store_initialized()
    if bool(args.prefetch_all_skill_wheels) or str(args.prefetch_skill_wheels).strip():
        payload = deps.prefetch_skill_wheels_cli(
            skill_name=(
                None
                if bool(args.prefetch_all_skill_wheels)
                else str(args.prefetch_skill_wheels).strip()
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if bool(args.queue_worker):
        raise SystemExit(asyncio.run(deps.run_queue_worker_loop()))
    if str(args.worker_run_id).strip():
        raise SystemExit(asyncio.run(deps.run_durable_worker(str(args.worker_run_id).strip())))
    deps.ensure_queue_worker_processes()
    deps.mcp.run()
