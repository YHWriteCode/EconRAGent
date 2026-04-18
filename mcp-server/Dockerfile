FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MCP_SKILLS_DIR=/app/skills \
    MCP_WORKSPACE_DIR=/workspace \
    MCP_RUNS_DIR=/workspace/runs \
    MCP_STATE_DIR=/workspace/state \
    MCP_ENVS_DIR=/workspace/envs \
    MCP_WHEELHOUSE_DIR=/workspace/wheelhouse \
    MCP_PIP_CACHE_DIR=/workspace/pip-cache \
    MCP_LOCKS_DIR=/workspace/locks

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY mcp-server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app /app/skills /workspace /workspace/runs /workspace/state /workspace/envs /workspace/wheelhouse /workspace/pip-cache /workspace/locks \
    && chown -R appuser:appuser /app /workspace

COPY mcp-server/server.py /app/server.py
COPY mcp_server_runtime /app/mcp_server_runtime
COPY kg_agent /app/kg_agent
COPY skills /app/skills

RUN chmod -R a-w /app/skills \
    && chown -R appuser:appuser /app/skills

USER appuser

VOLUME ["/workspace"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/server.py"]
