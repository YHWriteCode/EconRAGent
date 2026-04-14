from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from kg_agent.config import MCPCapabilityConfig, MCPConfig, MCPServerConfig
from kg_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)
DEFAULT_STDIO_STREAM_LIMIT_BYTES = max(
    65536,
    int(os.environ.get("KG_AGENT_MCP_STDIO_STREAM_LIMIT_BYTES", str(1024 * 1024))),
)


class MCPError(RuntimeError):
    pass


@dataclass
class _MCPServerSession:
    config: MCPServerConfig
    process: asyncio.subprocess.Process
    stdio_framing: str = "content_length"
    request_id: int = 0
    initialized: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPAdapter:
    def __init__(self, config: MCPConfig):
        self.config = config
        self._servers = {server.name: server for server in config.servers}
        self._capabilities = {cap.name: cap for cap in config.capabilities}
        self._sessions: dict[str, _MCPServerSession] = {}
        self._discovery_completed_servers: set[str] = set()

    def has_capabilities(self) -> bool:
        return bool(self._capabilities) or self.discovery_enabled()

    def discovery_enabled(self) -> bool:
        return self.config.discovery_enabled()

    def list_capability_configs(self) -> list[MCPCapabilityConfig]:
        return [self._capabilities[name] for name in sorted(self._capabilities)]

    def get_server_config(self, server_name: str) -> MCPServerConfig | None:
        return self._servers.get(server_name)

    async def discover_capabilities(
        self,
        *,
        reserved_names: set[str] | None = None,
    ) -> list[MCPCapabilityConfig]:
        if not self.discovery_enabled():
            return []

        used_names = set(reserved_names or set()) | set(self._capabilities)
        known_remote_keys = {
            (capability.server, capability.remote_name or capability.name)
            for capability in self._capabilities.values()
        }
        discovered: list[MCPCapabilityConfig] = []

        for server in self.config.servers:
            if not server.discover_tools or server.name in self._discovery_completed_servers:
                continue
            try:
                session = await self._get_session(server.name)
                tools = await self._list_tools(session)
            except Exception as exc:
                logger.warning(
                    "Failed to dynamically discover MCP tools from server %s: %s",
                    server.name,
                    exc,
                )
                continue

            for tool in tools:
                remote_name = str(tool.get("name", "")).strip()
                if not remote_name:
                    continue
                remote_key = (server.name, remote_name)
                if remote_key in known_remote_keys:
                    continue

                capability_name = self._allocate_capability_name(
                    server_name=server.name,
                    remote_name=remote_name,
                    used_names=used_names,
                )
                capability = MCPCapabilityConfig(
                    name=capability_name,
                    description=str(tool.get("description", "")).strip()
                    or f"MCP tool '{remote_name}' discovered from server '{server.name}'.",
                    input_schema=self._normalize_input_schema(tool),
                    server=server.name,
                    remote_name=remote_name,
                    enabled=True,
                    planner_exposed=False,
                    tags=["mcp", server.name],
                )
                self._capabilities[capability.name] = capability
                used_names.add(capability.name)
                known_remote_keys.add(remote_key)
                discovered.append(capability)

            self._discovery_completed_servers.add(server.name)

        return discovered

    async def invoke(self, capability_name: str, arguments: dict[str, Any]) -> ToolResult:
        capability = self._capabilities.get(capability_name)
        if capability is None:
            return ToolResult(
                tool_name=capability_name,
                success=False,
                error=f"MCP capability is not configured: {capability_name}",
            )
        if not capability.enabled:
            return ToolResult(
                tool_name=capability_name,
                success=False,
                error=f"MCP capability is disabled: {capability_name}",
            )

        try:
            session = await self._get_session(capability.server)
            result = await self._call_tool(
                session=session,
                remote_name=capability.remote_name or capability.name,
                arguments=self._sanitize_arguments(arguments),
            )
        except Exception as exc:
            return ToolResult(
                tool_name=capability_name,
                success=False,
                error=str(exc),
                metadata={"executor": "mcp", "server": capability.server},
            )

        structured_content = result.get("structuredContent")
        content = result.get("content")
        is_error = bool(result.get("isError"))
        summary = self._summarize_result(content=content, structured_content=structured_content)
        data = {
            "summary": summary,
            "structured_content": structured_content,
            "content": content,
            "raw": result,
        }
        return ToolResult(
            tool_name=capability_name,
            success=not is_error,
            data=data,
            error=summary if is_error else None,
            metadata={"executor": "mcp", "server": capability.server},
        )

    async def invoke_remote_tool(
        self,
        *,
        server_name: str,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        try:
            session = await self._get_session(server_name)
            result = await self._call_tool(
                session=session,
                remote_name=remote_name,
                arguments=self._sanitize_arguments(arguments),
            )
        except Exception as exc:
            return ToolResult(
                tool_name=remote_name,
                success=False,
                error=str(exc),
                metadata={"executor": "mcp", "server": server_name},
            )

        structured_content = result.get("structuredContent")
        content = result.get("content")
        is_error = bool(result.get("isError"))
        summary = self._summarize_result(content=content, structured_content=structured_content)
        data = {
            "summary": summary,
            "structured_content": structured_content,
            "content": content,
            "raw": result,
        }
        return ToolResult(
            tool_name=remote_name,
            success=not is_error,
            data=data,
            error=summary if is_error else None,
            metadata={"executor": "mcp", "server": server_name},
        )

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            process = session.process
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except Exception:
                    process.kill()
                    await process.wait()
        self._sessions.clear()

    async def _get_session(self, server_name: str) -> _MCPServerSession:
        session = self._sessions.get(server_name)
        if session is not None and session.process.returncode is None:
            return session

        config = self._servers.get(server_name)
        if config is None:
            raise MCPError(f"MCP server is not configured: {server_name}")
        if config.transport != "stdio":
            raise MCPError(f"Unsupported MCP transport: {config.transport}")

        framings = self._candidate_stdio_framings(config.stdio_framing)
        last_error: Exception | None = None
        for framing in framings:
            session = await self._launch_session(config=config, stdio_framing=framing)
            self._sessions[server_name] = session
            try:
                await self._initialize_session(session)
                return session
            except Exception as exc:
                last_error = exc
                await self._close_session(session)
                if self._sessions.get(server_name) is session:
                    self._sessions.pop(server_name, None)

        if last_error is not None:
            raise last_error
        raise MCPError(f"Failed to start MCP server session: {server_name}")

    async def _launch_session(
        self,
        *,
        config: MCPServerConfig,
        stdio_framing: str,
    ) -> _MCPServerSession:
        env = os.environ.copy()
        env.update(config.env)
        popen_kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
            "limit": DEFAULT_STDIO_STREAM_LIMIT_BYTES,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                config.command,
                *config.args,
                **popen_kwargs,
            )
        except TypeError:
            popen_kwargs.pop("limit", None)
            process = await asyncio.create_subprocess_exec(
                config.command,
                *config.args,
                **popen_kwargs,
            )
        return _MCPServerSession(
            config=config,
            process=process,
            stdio_framing=stdio_framing,
        )

    async def _close_session(self, session: _MCPServerSession) -> None:
        process = session.process
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                process.kill()
                await process.wait()

    async def _initialize_session(self, session: _MCPServerSession) -> None:
        if session.initialized:
            return
        response = await self._send_request(
            session,
            method="initialize",
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "kg_agent", "version": "0.1.0"},
            },
            timeout_s=session.config.startup_timeout_s,
        )
        if not isinstance(response, dict):
            raise MCPError("MCP initialize returned an invalid response")
        await self._send_notification(session, "notifications/initialized", {})
        session.initialized = True

    async def _call_tool(
        self,
        *,
        session: _MCPServerSession,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._send_request(
            session,
            method="tools/call",
            params={"name": remote_name, "arguments": arguments},
            timeout_s=session.config.tool_timeout_s,
        )
        if not isinstance(response, dict):
            raise MCPError("MCP tools/call returned an invalid response")
        return response

    async def _list_tools(
        self,
        session: _MCPServerSession,
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params = {"cursor": cursor} if cursor else {}
            response = await self._send_request(
                session,
                method="tools/list",
                params=params,
                timeout_s=session.config.tool_timeout_s,
            )
            if not isinstance(response, dict):
                raise MCPError("MCP tools/list returned an invalid response")

            batch = response.get("tools", [])
            if isinstance(batch, list):
                tools.extend(item for item in batch if isinstance(item, dict))

            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                break
            cursor = next_cursor.strip()

        return tools

    async def _send_notification(
        self,
        session: _MCPServerSession,
        method: str,
        params: dict[str, Any],
    ) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._write_message(session, message)

    async def _send_request(
        self,
        session: _MCPServerSession,
        *,
        method: str,
        params: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        async with session.lock:
            session.request_id += 1
            request_id = session.request_id
            await self._write_message(
                session,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            while True:
                message = await asyncio.wait_for(
                    self._read_message(session),
                    timeout=timeout_s,
                )
                if not isinstance(message, dict):
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise MCPError(str(message["error"]))
                return message.get("result", {})

    async def _write_message(
        self,
        session: _MCPServerSession,
        message: dict[str, Any],
    ) -> None:
        if session.process.stdin is None:
            raise MCPError("MCP server stdin is unavailable")
        encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
        if session.stdio_framing == "json_lines":
            session.process.stdin.write(encoded + b"\n")
        else:
            header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
            session.process.stdin.write(header + encoded)
        await session.process.stdin.drain()

    async def _read_message(self, session: _MCPServerSession) -> dict[str, Any]:
        if session.process.stdout is None:
            raise MCPError("MCP server stdout is unavailable")

        content_length = None
        while True:
            line = await session.process.stdout.readline()
            if not line:
                stderr_output = await self._read_stderr(session)
                raise MCPError(
                    "MCP server closed the connection"
                    + (f": {stderr_output}" if stderr_output else "")
                )
            stripped = line.strip()
            if stripped.startswith(b"{") or stripped.startswith(b"["):
                return json.loads(stripped.decode("utf-8"))
            decoded = line.decode("ascii", errors="ignore").strip()
            if not decoded:
                break
            if decoded.lower().startswith("content-length:"):
                content_length = int(decoded.split(":", 1)[1].strip())

        if content_length is None:
            raise MCPError("MCP message is missing Content-Length")
        payload = await session.process.stdout.readexactly(content_length)
        return json.loads(payload.decode("utf-8"))

    @staticmethod
    def _candidate_stdio_framings(raw_value: str) -> list[str]:
        normalized = str(raw_value or "").strip().lower()
        if normalized in {"jsonl", "json_lines", "json-lines", "line", "line_json"}:
            return ["json_lines"]
        if normalized in {"content_length", "content-length", "header"}:
            return ["content_length"]
        return ["content_length", "json_lines"]

    async def _read_stderr(self, session: _MCPServerSession) -> str:
        if session.process.stderr is None:
            return ""
        chunks = []
        while True:
            line = await session.process.stderr.readline()
            if not line:
                break
            chunks.append(line.decode("utf-8", errors="ignore").strip())
            if len(chunks) >= 5:
                break
        return " | ".join(item for item in chunks if item)

    @staticmethod
    def _summarize_result(
        *,
        content: Any,
        structured_content: Any,
    ) -> str:
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        if isinstance(structured_content, dict):
            for key in ("summary", "message", "status"):
                value = structured_content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "MCP capability executed"

    @staticmethod
    def _normalize_input_schema(tool_payload: dict[str, Any]) -> dict[str, Any]:
        input_schema = tool_payload.get("inputSchema")
        if not isinstance(input_schema, dict):
            input_schema = tool_payload.get("input_schema")
        if isinstance(input_schema, dict):
            return input_schema
        return {"type": "object"}

    @staticmethod
    def _allocate_capability_name(
        *,
        server_name: str,
        remote_name: str,
        used_names: set[str],
    ) -> str:
        candidates = [remote_name, f"{server_name}.{remote_name}"]
        for candidate in candidates:
            if candidate not in used_names:
                return candidate

        suffix = 2
        while True:
            candidate = f"{server_name}.{remote_name}_{suffix}"
            if candidate not in used_names:
                return candidate
            suffix += 1

    @classmethod
    def _sanitize_arguments(cls, value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return cls._sanitize_arguments(dataclasses.asdict(value))
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [item for item in (cls._sanitize_arguments(item) for item in value) if item is not _DROP]
        if isinstance(value, tuple):
            return [item for item in (cls._sanitize_arguments(item) for item in value) if item is not _DROP]
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                normalized = cls._sanitize_arguments(item)
                if normalized is _DROP:
                    continue
                sanitized[key] = normalized
            return sanitized
        return _DROP


_DROP = object()
