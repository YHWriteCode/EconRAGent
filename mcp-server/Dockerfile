FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MCP_SKILLS_DIR=/app/skills \
    MCP_WORKSPACE_DIR=/workspace

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY mcp-server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app /app/skills /workspace \
    && chown -R appuser:appuser /app /workspace

COPY mcp-server/server.py /app/server.py
COPY kg_agent /app/kg_agent
COPY skills /app/skills

RUN chmod -R a-w /app/skills \
    && chown -R appuser:appuser /app/skills

USER appuser

VOLUME ["/workspace"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/server.py"]
