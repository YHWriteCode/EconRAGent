## 1. 项目简介

**EconRAGent** 是一个面向经济金融领域的 RAG（Retrieval-Augmented Generation）智能分析助手，具备知识图谱构建、多模式检索、Agent 推理编排、网络爬虫定时摄入及本地技能执行等能力。

**核心特性：**
- 基于知识图谱（KG）+ 向量数据库的混合检索，专为经济金融分析场景优化
- Agent 主循环：路由判断 → 工具调用 → 路径解释 → 最终答案
- 统一 WebUI：对话 / 知识图谱 / 发现 / 空间管理四页同源部署
- 支持 Redis 分布式锁、时序元数据维护（`created_at`/`last_confirmed_at`/`confirmation_count`）
- 动态图谱感知新鲜度排序（Freshness-Aware Ranking）
- 定时爬虫 + Feed 感知内容摄入调度
- 本地技能（Skills）+ 外部 MCP 能力扩展

---

## 2. 整体架构

```
┌─────────────────────────────────────────┐
│              外部接口层                  │
│  kg_agent/api/  (FastAPI, REST API)      │
└────────────────┬────────────────────────┘
                 │
┌────────────────▼────────────────────────┐
│           业务 Agent 层 (kg_agent/)      │
│  AgentCore / RouteJudge / PathExplainer  │
│  ToolRegistry / CapabilityRegistry       │
│  Skills / MCP Adapter                    │
│  Memory / Crawler / Scheduler            │
└────────────────┬────────────────────────┘
                 │ 单向依赖
                 ▼
┌─────────────────────────────────────────┐
│     图谱+向量后端层 (lightrag_fork/)     │
│  文档分块 / 实体抽取 / 图谱合并 / 写入   │
│  混合检索 / 分布式锁 / 领域 Schema       │
│  存储后端：Neo4j + Qdrant + MongoDB      │
└─────────────────────────────────────────┘
```

**依赖方向严格单向：**
```
kg_agent/ ──依赖──> lightrag_fork/
lightrag_fork/ ──禁止依赖──> kg_agent/
```

---

## 3. 目录结构总览

```
EconRAGent/
├── lightrag_fork/          # 图谱+向量后端（基于 HKUDS/LightRAG 最小侵入增强）
│   ├── lightrag.py         # 主入口：初始化、文档插入、查询
│   ├── operate.py          # 核心执行：分块、实体抽取、图谱合并、检索
│   ├── schema.py           # 领域 Schema 配置与归一化
│   ├── schemas/            # 内置 Schema（general / economy）
│   ├── kg/                 # 存储后端实现（Neo4j / Qdrant / MongoDB 等）
│   ├── llm/                # LLM 调用适配层（OpenAI / Ollama / Azure 等）
│   ├── api/                # LightRAG REST API 服务
│   └── AGENTS.md           # 后端层详细规范
│
├── kg_agent/               # 业务 Agent 层
│   ├── agent/              # Agent 核心（AgentCore / RouteJudge / PathExplainer）
│   ├── skills/             # 本地技能目录（SkillRegistry / SkillExecutor）
│   ├── mcp/                # 外部 MCP 能力传输层
│   ├── tools/              # 工具集（检索 / 图谱 / 爬虫 / 摄入）
│   ├── memory/             # 记忆系统（会话 / 跨会话 / 用户档案）
│   ├── crawler/            # 网络爬虫层（Crawl4AI + Feed 调度）
│   ├── api/                # kg_agent REST API 服务
│   └── AGENTS.md           # 业务层详细规范
│
├── EconRAGent_webui/       # React + TypeScript + Vite 前端源码
│   ├── src/                # 页面、组件、状态管理与测试
│   ├── package.json        # 前端依赖与脚本
│   ├── package-lock.json   # 前端锁文件（应提交）
│   └── AGENTS.md           # 前端开发规范
│
├── mcp-server/             # 独立 MCP 技能运行时服务（Docker 化）
│   ├── server.py           # FastMCP stdio 入口，durable SQLite 队列
│   └── Dockerfile          # 容器镜像（预置金融依赖：numpy/pandas/yfinance/akshare 等）
│
├── skills/                 # 本地技能定义目录（每个技能含 SKILL.md）
│   └── financial-researching/  # 示例：金融研究技能（面板数据/回测/AKShare）
│
├── tests/                  # 测试
│   ├── kg_agent/           # Agent 层集成测试
│   └── (lightrag_fork/tests/e2e/ 位于子模块内)
│
├── .env.example            # 环境变量模板
├── AGENTS.md               # 仓库级协作说明
└── README.md               # 本文件（项目总览）
```

---

## 4. 两大子模块职责对照

| 维度 | `lightrag_fork/` | `kg_agent/` |
|---|---|---|
| **定位** | 图谱+向量纯后端 | 业务 Agent 编排层 |
| **核心职责** | 文档分块、实体/关系抽取、图谱合并、向量写入、混合检索 | Agent 主循环、路由判断、工具调用、路径解释、记忆管理 |
| **对外接口** | `from lightrag_fork import LightRAG, QueryParam` | `AgentCore.chat()` / FastAPI `/agent/*` |
| **禁止事项** | 不得引入 Agent 编排、会话记忆、用户档案、工具调用等业务代码 | 不得直接修改 `lightrag_fork/` 中的文件 |
| **存储后端** | Neo4j（图谱）+ Qdrant（向量）+ MongoDB（KV/DocStatus）| 复用上层存储 + SQLite（会话记忆/技能运行状态） |
| **LLM 依赖** | 实体抽取、摘要生成、查询扩展 | 路由判断、路径解释、最终答案生成 |

---

## 5. 核心数据流

### 5.1 文档摄入流（Insert）

```
用户调用 rag.ainsert(documents)
  → lightrag_fork: 文档去重、filter_keys
  → operate.py: 文本分块（chunking_by_token_size）
  → operate.py: LLM 实体/关系抽取（extract_entities）
      ├─ 可注入领域 Schema 约束（economy / general）
      └─ Schema 后处理归一化
  → operate.py: 图谱合并（merge_nodes_and_edges）
      └─ 更新时序元数据（created_at / last_confirmed_at / confirmation_count）
  → kg/*_impl: 写入 Neo4j / Qdrant / MongoDB
```

### 5.2 查询对话流（Chat）

```
用户 → POST /agent/chat
  → AgentCore.chat()
      1. 加载上下文（会话历史 / 用户档案 / 可用能力/技能清单）
      2. RouteJudge.plan() → RouteDecision（路由决策）
      3. 顺序执行工具序列（kg_hybrid_search / graph_entity_lookup 等）
      4. [可选] 本地技能执行（SkillExecutor）
      5. [可选] 动态图谱桥接（freshness_aware_search / correction_and_refresh）
      6. [可选] PathExplainer.explain() → 图谱路径解释
      7. LLM 生成最终答案
      8. 持久化会话记忆
  → 返回 ChatResponse（answer / route / tool_calls / path_explanation / metadata）
```

### 5.3 检索模式

| 模式 | 描述 |
|---|---|
| `naive` | 仅对文本块做向量检索 |
| `local` | 以实体为中心的子图 + 关系上下文 |
| `global` | 以关系为中心的高阶语义检索 |
| `hybrid` | local + global 合并 |
| `mix` | hybrid + naive 合并 |

---

## 6. 领域 Schema

项目内置两种领域 Schema，均定义于 `lightrag_fork/schemas/`：

| Schema | 文件 | 实体类型 | 默认状态 |
|---|---|---|---|
| `general` | `schemas/general.py` | Person, Organization, Location... | `enabled=False`（保持原始 LightRAG 行为） |
| `economy` | `schemas/economy.py` | Company, Industry, Metric, Policy, Event, Asset, Institution, Country | `enabled=True, mode=domain` |

每个 Schema 还附带 `explanation_profile`，供上层 PathExplainer 消费意图触发、语义标签、关系语义、节点角色规则、路径约束、证据策略等信息，无需在 `kg_agent` 中硬编码。

---

## 7. 工具与能力体系

### 7.1 内置原生工具（Native Tools）

| 工具名 | 标签 | 默认启用 |
|---|---|---|
| `kg_hybrid_search` | retrieval, knowledge-graph | ✅ |
| `kg_naive_search` | retrieval, vector | ✅ |
| `graph_entity_lookup` | graph | ✅ |
| `graph_relation_trace` | graph, explanation | ✅ |
| `memory_search` | memory | ✅（受 `KG_AGENT_ENABLE_MEMORY` 控制） |
| `cross_session_search` | memory, cross-session | ✅（需要 user_id） |
| `kg_ingest` | knowledge-graph, ingestion | ✅（受 `KG_AGENT_ENABLE_KG_INGEST` 控制） |
| `web_search` | web | ❌（需 `KG_AGENT_ENABLE_WEB_SEARCH=true`） |

### 7.2 外部能力扩展（MCP）

- 通过 `KG_AGENT_MCP_SERVERS_JSON` 配置 stdio MCP 服务器
- 通过 `KG_AGENT_MCP_CAPABILITIES_JSON` 声明静态能力或开启动态发现
- MCP 能力默认对规划器不可见（`planner_exposed=false`），需显式设置才参与自动路由

### 7.3 本地技能（Skills）

- 放置于 `skills/*/SKILL.md` 目录下，由 `SkillRegistry` 自动扫描
- 支持 `conservative`（保守执行）和 `free_shell`（LLM 辅助 Shell 规划）两种执行模式
- 通过 `mcp-server/` 容器化运行时实现隔离执行、可持久化队列（SQLite）、工件追踪
- 内置技能示例：`skills/financial-researching/`（面板数据建模、AKShare/yfinance 行情获取、本地回测）

---

## 8. 记忆系统

| 组件 | 说明 | 支持后端 |
|---|---|---|
| `ConversationMemoryStore` | 会话内记忆，动态注意力窗口 + 查询感知回填 | memory / sqlite / mongo |
| `CrossSessionStore` | 跨会话检索，同一用户历史语义搜索 | memory / mongo_qdrant |
| `UserProfileStore` | 用户档案存储 | memory / sqlite / mongo |

---

## 9. 定时爬虫与摄入调度

`kg_agent/crawler/` 提供完整的网页 + Feed 定时摄入基础设施：

- **Crawl4AI** 作为爬虫后端（支持 Playwright 浏览器 + 系统 Chrome/Edge 回退）
- **DuckDuckGo** 搜索结果 URL 发现与质量打分
- **Feed 感知调度**：自动识别 RSS/Atom，支持 `adaptive_feed` 自适应轮询间隔
- **内容生命周期**：`short_term_news`（替换最新版）vs `long_term_knowledge`（追加）
- **事件聚合**（Event Clustering）：启用 Utility LLM 时自动合并同一事件的多篇文章
- **内容去重**：`content_hash` / `content_signature` 两种去重策略

---

## 10. 生产存储组合

| 存储类型 | 生产推荐 | 本地开发 |
|---|---|---|
| 图谱 | Neo4j | NetworkX（本地文件） |
| 向量 | Qdrant / Milvus | NanoVectorDB / Faiss |
| KV / DocStatus | MongoDB / Redis | JSON 文件 |
| 分布式锁 | Redis（`LIGHTRAG_LOCK_BACKEND=redis`） | 本地锁（默认） |

**当前项目生产组合：** Neo4j + Qdrant + MongoDB

---

## 11. 快速启动

### 11.1 环境要求

在项目根目录创建 `.env`（参考 `.env.example`），至少包含：

```dotenv
# LLM 配置
LLM_MODEL=your-model-name
LLM_BINDING=openai_compatible
LLM_BINDING_HOST=https://your-llm-endpoint
LLM_BINDING_API_KEY=your-api-key

# 可选：轻量级 Utility LLM（用于路由判断/路径解释）
UTILITY_LLM_MODEL=your-utility-model
UTILITY_LLM_BINDING_HOST=https://your-utility-endpoint

# 存储配置
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
QDRANT_URL=http://localhost:6333
MONGO_URI=mongodb://localhost:27017
MONGO_DATABASE=lightrag
```

### 11.2 启动命令

```bash
# 激活虚拟环境
.venv\Scripts\Activate.ps1       # Windows PowerShell
source .venv/bin/activate          # Linux/Mac

# 启动 kg_agent API 服务（默认端口 9721）
kg-agent-server
# 或
uv run kg-agent-server
# 或
python -m kg_agent.api.app

# 启动 lightrag_fork 独立 API 服务
python -m lightrag_fork.api.lightrag_server

# 构建 MCP 技能运行时容器镜像
docker build -f mcp-server/Dockerfile -t econragent-mcp-skill-service:latest .
```

### 11.2.1 WebUI 前端开发与构建

前端源码位于 `EconRAGent_webui/`，构建产物输出到 `kg_agent/api/webui/`，由 `kg_agent` 统一挂载 `/webui` 提供静态页面。

```bash
cd EconRAGent_webui

# 安装前端依赖
npm install

# 运行前端测试
node .\node_modules\vitest\vitest.mjs run

# 构建前端静态产物到 kg_agent/api/webui/
node .\node_modules\vite\bin\vite.js build
```

前端协作规则：

- `EconRAGent_webui/node_modules/` 是本地依赖目录，不应提交到 git
- `EconRAGent_webui/package-lock.json` 应提交，用于锁定已验证的依赖版本
- 修改前端源码后，应同步刷新 `kg_agent/api/webui/` 构建产物，否则后端打包出来的静态页面会落后于源码
- WebUI 默认页面入口包括：
  - `/webui/chat`
  - `/webui/graph`
  - `/webui/discover`
  - `/webui/spaces`

### 11.2.2 Skill Runtime 持久化与依赖预热

当前 `mcp-server` 已改为：

- Skill 依赖写在各自目录下的 `requirements.lock`
- 运行时按 `env_hash` 复用 `/workspace/envs/<hash>`
- `runs/state/envs/wheelhouse/pip-cache/locks` 默认使用 Docker named volume 持久化
- 共享输出目录默认挂载到项目根目录下的 `skill_output/`

仓库内已经提供可直接在 Windows PowerShell 上使用的宿主脚本：

```powershell
# 生成使用 Docker volume 的 KG_AGENT_MCP_SERVERS_JSON
powershell -ExecutionPolicy Bypass -File .\mcp-server\scripts\init-skill-runtime-host.ps1 `
  -ConfigOutputPath .\kg-agent-mcp-servers.json

# 预热全部 skill 的 wheel 包（写入 named volume，不安装）
powershell -ExecutionPolicy Bypass -File .\mcp-server\scripts\prefetch-skill-wheels.ps1 `
  -All

# 只预热单个 skill
powershell -ExecutionPolicy Bypass -File .\mcp-server\scripts\prefetch-skill-wheels.ps1 `
  -SkillName pdf
```

`init-skill-runtime-host.ps1` 现在默认输出使用 Docker named volume 的 `KG_AGENT_MCP_SERVERS_JSON`，避免不同宿主机上的路径格式差异；只有共享输出目录会绑定到仓库根目录下的 `skill_output/`。

如果是本地开发调试，不想每次都重建镜像，可以追加 `-SourceRoot D:\AllForLearning\EconRAGent`。这样容器会直接挂载当前仓库源码，执行 `/src/mcp-server/server.py`，镜像只负责提供基础运行时环境。

首次部署后，建议先预热 wheel 缓存：

```bash
# 预热全部 skill 的 wheel 包到 named volume（不安装）
docker run --rm \
  -v mcp_skill_wheelhouse:/workspace/wheelhouse \
  -v mcp_skill_pip_cache:/workspace/pip-cache \
  -v mcp_skill_state:/workspace/state \
  -v mcp_skill_locks:/workspace/locks \
  econragent-mcp-skill-service:latest \
  python /app/server.py --prefetch-all-skill-wheels
```

`KG_AGENT_MCP_SERVERS_JSON` 可按下面方式配置，将 `runs/state/envs/wheelhouse/pip-cache/locks` 放在 Docker volume 中，并把共享输出绑定到仓库根目录 `skill_output/`：

```json
[
  {
    "name": "skill-runtime",
    "command": "docker",
    "stdio_framing": "json_lines",
    "args": [
      "run",
      "--rm",
      "-i",
      "-e", "MCP_SKILLS_DIR=/app/skills",
      "-e", "MCP_WORKSPACE_DIR=/workspace",
      "-e", "MCP_RUNS_DIR=/workspace/runs",
      "-e", "MCP_STATE_DIR=/workspace/state",
      "-e", "MCP_ENVS_DIR=/workspace/envs",
      "-e", "MCP_OUTPUT_DIR=/workspace/output",
      "-e", "MCP_WHEELHOUSE_DIR=/workspace/wheelhouse",
      "-e", "MCP_PIP_CACHE_DIR=/workspace/pip-cache",
      "-e", "MCP_LOCKS_DIR=/workspace/locks",
      "-v", "/your/repo/skill_output:/workspace/output",
      "-v", "mcp_skill_runs:/workspace/runs",
      "-v", "mcp_skill_state:/workspace/state",
      "-v", "mcp_skill_envs:/workspace/envs",
      "-v", "mcp_skill_wheelhouse:/workspace/wheelhouse",
      "-v", "mcp_skill_pip_cache:/workspace/pip-cache",
      "-v", "mcp_skill_locks:/workspace/locks",
      "econragent-mcp-skill-service:latest",
      "python",
      "/app/server.py"
    ],
    "discover_tools": false
  }
]
```

推荐顺序：

1. `docker build -f mcp-server/Dockerfile -t econragent-mcp-skill-service:latest .`
2. 运行 `init-skill-runtime-host.ps1`
3. 运行 `prefetch-skill-wheels.ps1`
4. 将脚本输出的 JSON 设置到 `KG_AGENT_MCP_SERVERS_JSON`

如果当前机器无法访问 Docker Hub，但本地已经有旧版 `econragent-mcp-skill-service:latest`，可以改用离线 fallback：

```bash
docker build -f mcp-server/Dockerfile.local-rebuild -t econragent-mcp-skill-service:latest .
```

这个 fallback 会基于本机现有镜像重建，覆盖最新的 `server.py`、`kg_agent/` 和 `skills/`，并补齐新的 `MCP_*_DIR` 默认环境变量与持久化目录。它的目标是本地开发/测试可继续推进，不用于替代正式的全量基础镜像构建。

### 11.3 主要 API 端点

| 方法 | 路径 | 描述 |
|---|---|---|
| POST | `/agent/chat` | 主对话入口（支持 SSE 流式） |
| POST | `/agent/ingest` | 向知识图谱摄入内容 |
| POST | `/agent/uploads` | 上传聊天或导入附件 |
| GET | `/agent/sessions` | 按 workspace 查询会话摘要 |
| GET | `/agent/workspaces` | 查询知识库空间列表 |
| GET | `/agent/graph/overview` | 查询图谱概览/联邦概览 |
| GET | `/agent/discover/events` | 查询发现页事件流 |
| GET | `/agent/graph/schema` | 查询 WebUI 图谱筛选使用的实体/关系 Schema 模板 |
| GET | `/agent/tools` | 查看当前可用工具/能力 |
| GET | `/agent/skills` | 查看本地技能目录 |
| POST | `/agent/skills/{skill_name}/invoke` | 直接调用本地技能 |
| GET | `/agent/skill-runs/{run_id}` | 查询技能运行状态 |
| POST | `/agent/capabilities/{name}/invoke` | 直接调用单个能力 |
| GET | `/agent/sources` | 查看监控爬取来源 |
| POST | `/agent/sources` | 添加/更新监控来源 |
| GET | `/webui/chat` | WebUI 对话页入口 |
| GET | `/health` | 健康检查 |

---

## 12. 关键测试

```bash
# 后端 E2E 测试（需 Neo4j / Qdrant / MongoDB / LLM 服务）
python -m lightrag_fork.tests.e2e.test_pipeline_e2e

# Schema 对比测试
python -m lightrag_fork.tests.e2e.debug_schema_compare

# Agent 查询链测试
python -m pytest tests/kg_agent/test_query_agent_answer_chain.py

# Web 摄入链测试
python -m pytest tests/kg_agent/test_web_ingest_chain.py

# Feed 调度链测试
python -m pytest tests/kg_agent/test_feed_scheduler_chain.py
```

---

## 13. 架构约束总结

| 约束 | 说明 |
|---|---|
| **单向依赖** | `kg_agent/` 可依赖 `lightrag_fork/`，反向绝对禁止 |
| **最小侵入** | `lightrag_fork/` 对上游 HKUDS/LightRAG 保持最小侵入修改 |
| **向后兼容** | 所有新特性默认关闭，未提供新配置时行为与原始 LightRAG 一致 |
| **无 LangChain** | Agent 循环、工具调用、Prompt 组装全部自建，不引入重量级框架 |
| **业务逻辑归位** | 爬虫、会话记忆、用户档案、工具调用只在 `kg_agent/` 中实现 |
| **存储逻辑归位** | Neo4j/Qdrant/MongoDB 后端管理只在 `lightrag_fork/kg/` 中实现 |

---

## 14. 子文档索引

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 仓库级协作规范，包括 `.venv` 使用和前端依赖管理约束 |
| [`lightrag_fork/AGENTS.md`](lightrag_fork/AGENTS.md) | 后端层完整架构、存储后端配置、分布式锁、领域 Schema、时序元数据、开发命令 |
| [`kg_agent/AGENTS.md`](kg_agent/AGENTS.md) | Agent 层完整架构、路由判断、路径解释、工具注册、技能系统、记忆系统、API 端点、配置参数 |
| [`EconRAGent_webui/AGENTS.md`](EconRAGent_webui/AGENTS.md) | WebUI 前端源码、依赖管理、测试与构建规范 |
| [`lightrag_fork/docs/CHANGES.md`](lightrag_fork/docs/CHANGES.md) | 后端层完整变更日志 |
| [`.env.example`](.env.example) | 环境变量配置模板 |

## Acknowledge
本项目知识图谱和RAG基于[LightRAG](https://github.com/HKUDS/LightRAG),
