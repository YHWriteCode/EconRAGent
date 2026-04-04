# 动态图谱模块状态文档

> 用途：作为当前仓库动态图谱实现状态的单一说明文档。  
> 基线：以 `kg_agent -> lightrag_fork` 的双层架构为边界，不回退重写已落地框架。  
> 更新时间：2026-04-02

---

## 0. 全局约束

- [x] 已阅读 `lightrag_fork/AGENTS.md`，底层只承载图谱、向量、检索与存储能力。
- [x] 已阅读 `kg_agent/AGENTS.md`，业务层承载 Agent 编排、scheduler、memory、API、bridge 逻辑。
- [x] 依赖方向固定为 `kg_agent -> lightrag_fork`。
- [x] 新增功能默认关闭，不改变普通问答默认路径。
- [x] 未引入 LangChain、未引入新 ORM、未修改 `prompt.py` 静态模板。
- [x] 图谱溯源继续复用 `file_path`，未新增 `source_urls`。
- [x] 当前执行器已支持通用 tool chaining / output piping；`web_search -> kg_ingest` 可通过 `ToolCallPlan.input_bindings` 正常串联，动态刷新场景仍可复用 `AgentCore` bridge。
- [x] `rag.ainsert().file_paths` 继续兼容 `str | list[str] | None`。

---

## 1. 关键决策

- `web_search -> kg_ingest` 保留为目标链路，现已同时支持通用 output piping 与 `AgentCore` bridge 两种执行方式。
- `confirmation_count` 在 v1 语义固定为“累计确认事件次数”，不是“独立来源数”。
- scheduler 已支持基于 source lease 的多 worker / 多实例协调，并新增可选 loop leader election。
- Phase D2 已下沉到 `lightrag_fork/operate.py` 的 KG 检索排序主链路，retrieval tool 层仅保留兼容性 fallback。
- `sources_file=""` 时 source API 只保存在内存；非空时必须 JSON 落盘并支持重启恢复。

---

## 2. Phase D1：时间元数据写入与透传

### 2.1 当前状态

- [x] `lightrag_fork/operate.py` 已在主要 node / edge merge 路径中维护：
  - `created_at`
  - `last_confirmed_at`
  - `confirmation_count`
- [x] 新建节点/边会初始化 `created_at`、`last_confirmed_at`、`confirmation_count=1`。
- [x] 合并已有节点/边时保留旧 `created_at`，更新 `last_confirmed_at`，递增 `confirmation_count`。
- [x] 关系 rebuild 路径中补建缺失节点时，现在也会初始化 `last_confirmed_at` 与 `confirmation_count`。
- [x] 查询返回链路已透传到用户格式：
  - `entities[].created_at`
  - `entities[].last_confirmed_at`
  - `entities[].confirmation_count`
  - `entities[].rank`
  - `relationships[].created_at`
  - `relationships[].last_confirmed_at`
  - `relationships[].confirmation_count`
  - `relationships[].rank`
- [x] 老数据缺字段时按 `None` / 缺省容错，不抛错。

### 2.2 验收结论

- D1 已完成。

---

## 3. Phase A：scheduler + source/state + 增量入库

### 3.1 当前状态

- [x] 已新增 `MonitoredSource` 与 `JsonSourceRegistry`。
- [x] 已新增 `CrawlStateRecord` 与 `JsonCrawlStateStore`。
- [x] `kg_agent/crawler/scheduler.py` 已由占位实现升级为 `IngestScheduler`。
- [x] scheduler 已支持：
  - `start()`
  - `stop()`
  - `list_sources()`
  - `add_source()`
  - `remove_source()`
  - `trigger_now()`
  - `get_status()`
- [x] `_poll_source()` 使用 `crawler_adapter.crawl_urls(...)` 抓取页面。
- [x] 使用 `compute_mdhash_id(page.markdown)` 做内容 hash 去重。
- [x] 同 URL 同内容不会重复 `ainsert`。
- [x] 内容变化后会重新 `ainsert`。
- [x] `app.py` 已通过 lifespan 接入 scheduler 启停。
- [x] 已开放接口：
  - `GET /agent/scheduler/status`
  - `GET /agent/sources`
  - `POST /agent/sources`
  - `DELETE /agent/sources/{source_id}`
  - `POST /agent/sources/{source_id}/trigger`
- [x] `KG_AGENT_ENABLE_SCHEDULER`
- [x] `KG_AGENT_SCHEDULER_CHECK_INTERVAL`
- [x] `KG_AGENT_SCHEDULER_SOURCES_FILE`
- [x] `KG_AGENT_SCHEDULER_STATE_FILE`

### 3.2 v1 边界

- [x] 仅支持直接网页 URL。
- [x] RSS / Feed 解析已支持 direct feed URL 展开并抓取文章页。
- [x] scheduler 直接调用 `rag.ainsert(...)`，不强制改为走 `kg_ingest` 工具。
- [x] 不做多实例协调。

### 3.3 验收结论

- A 已完成，按单进程 v1 交付。

---

## 4. Phase C0：tool call history 持久化

### 4.1 当前状态

- [x] `ConversationMemoryStore` 仍以 message 为基本存储单位。
- [x] assistant message metadata 已写入 `compact_tool_calls`。
- [x] 每条 compact tool call 只保留：
  - `tool`
  - `success`
  - `summary`
  - `strategy`
  - `timestamp`
- [x] 不写入完整 tool result data，不写入 page markdown。
- [x] `AgentCore.chat()` 结束后会把 compact tool history 与 assistant answer 一起写入 memory。
- [x] `preview_route()` 与 `chat()` 构造 `session_context` 时会注入 `recent_tool_calls`。
- [x] 当前默认只注入最近 1 个 assistant turn 的 tool calls。

### 4.2 验收结论

- C0 已完成。

---

## 5. Phase B：对话驱动按需刷新

### 5.1 当前状态

- [x] `RouteJudge` 已支持 `freshness_aware_search`。
- [x] realtime 路由默认采用：
  - `web_search`
  - `kg_hybrid_search`
- [x] `kg_ingest` 不放入 `tool_sequence`，避免伪装成通用工具串联。
- [x] `AgentCore` 已实现 freshness check：
  - 图谱无结果时视为 `gap`
  - 图谱时间戳平均年龄超过阈值时视为 `stale`
  - 新鲜时返回 `graph_data_fresh`
- [x] 启用 `KG_AGENT_ENABLE_AUTO_INGEST=true` 时，会把 `web_search.pages` bridge 成 `kg_ingest(content/source)`。
- [x] bridge 只 ingest 成功页面，source 优先取 `final_url`，否则退回原始 URL。
- [x] metadata 已输出：
  - `freshness_action`
  - `freshness_reason`
- [x] 已新增配置：
  - `KG_AGENT_FRESHNESS_THRESHOLD_SECONDS`
  - `KG_AGENT_ENABLE_AUTO_INGEST`

### 5.2 验收结论

- B 已完成。

---

## 6. Phase C：用户纠错驱动刷新

### 6.1 当前状态

- [x] `RouteJudge` 已支持 `CORRECTION_PATTERN`。
- [x] 纠错触发依赖 `recent_tool_calls`，不是只靠文本 history 猜测。
- [x] 只有“当前 query 命中纠错模式”且“上一轮确实用过 KG 检索工具”时，才进入 `correction_and_refresh`。
- [x] 纠错刷新链路继续是 `web_search -> kg_ingest`，由 `AgentCore` bridge。
- [x] correction target 默认优先取上一轮用户 query；取不到时退回 assistant 文本。
- [x] 最终回答会附加刷新说明。
- [x] metadata 已输出：
  - `freshness_action=user_correction_refresh`
  - `freshness_reason`
- [x] correction refresh 不依赖 `enable_auto_ingest`，但仍要求 `kg_ingest` 工具可用。

### 6.2 验收结论

- C 已完成。

---

## 7. Phase D2：检索时 freshness decay

### 7.1 当前状态

- [x] 已新增配置：
  - `KG_AGENT_STALENESS_DECAY_DAYS`
  - `KG_AGENT_ENABLE_FRESHNESS_DECAY`
- [x] 当前实现位置在 `kg_agent/tools/retrieval_tools.py`。
- [x] 仅在开关开启时，对 `entities` / `relationships` 按 `rank + last_confirmed_at` 做 v1 重排。
- [x] 老数据缺失 `last_confirmed_at` 时退回原始 `rank`，不报错。

### 7.2 当前实现说明

- [x] 已下沉到 `lightrag_fork/operate.py` 的 KG 检索排序主链路。
- [x] freshness 已参与 local/global/hybrid/mix 的底层排序融合，retrieval tool 层仅保留兼容性 fallback。

### 7.3 验收结论

- D2 已完成并下沉到底层 KG 检索链路，当前版本仍属于启发式排序增强而非完整排序模型重构。

---

## 8. 测试状态

### 已覆盖

- [x] scheduler 同内容不重复入库、内容变化后重新入库。
- [x] source/state JSON 持久化后重启可恢复。
- [x] `AgentCore.chat()` 会写入 `compact_tool_calls`。
- [x] 下一轮 `preview_route/chat` 可读取 `recent_tool_calls`。
- [x] stale graph 可触发 `auto_ingested`。
- [x] fresh graph 可触发 `graph_data_fresh`。
- [x] correction 可触发 `user_correction_refresh` 并附带回答说明。
- [x] `convert_to_user_format()` 会透传时间元数据和 `rank`。
- [x] retrieval freshness decay 开启时会把更新鲜结果排前。

### 最近一次相关测试结果

- `tests/kg_agent/test_agent_core.py`
- `tests/kg_agent/test_app_bootstrap.py`
- `tests/kg_agent/test_route_judge.py`
- `tests/kg_agent/test_conversation_memory.py`
- `tests/kg_agent/test_dynamic_graph_framework.py`
- `tests/kg_agent/test_scheduler_framework.py`

结果：通过。

---

## 9. 环境变量总表

| 变量 | 默认值 | 用途 |
|---|---|---|
| `KG_AGENT_ENABLE_SCHEDULER` | `false` | 启用 scheduler |
| `KG_AGENT_SCHEDULER_CHECK_INTERVAL` | `60` | scheduler 主循环检查间隔 |
| `KG_AGENT_SCHEDULER_SOURCES_FILE` | `""` | source 持久化文件；为空时仅内存态 |
| `KG_AGENT_SCHEDULER_STATE_FILE` | `"scheduler_state.json"` | crawl state 持久化文件 |
| `KG_AGENT_SCHEDULER_ENABLE_LEADER_ELECTION` | `false` | 是否启用 scheduler loop leader election |
| `KG_AGENT_SCHEDULER_LOOP_LEASE_KEY` | `"scheduler:loop"` | scheduler 主循环所有权协调 key |
| `KG_AGENT_SCHEDULER_COORDINATION_BACKEND` | `"auto"` | scheduler 协调后端：`auto/local/redis` |
| `KG_AGENT_SCHEDULER_COORDINATION_REDIS_URL` | `""` | Redis 协调地址 |
| `KG_AGENT_SCHEDULER_COORDINATION_TTL_SECONDS` | `120` | scheduler 协调租约 TTL |
| `KG_AGENT_FRESHNESS_THRESHOLD_SECONDS` | `604800` | freshness 判定阈值 |
| `KG_AGENT_ENABLE_AUTO_INGEST` | `false` | 是否允许对话中自动入库 |
| `KG_AGENT_STALENESS_DECAY_DAYS` | `7.0` | freshness-aware KG retrieval decay 半衰期参数 |
| `KG_AGENT_ENABLE_FRESHNESS_DECAY` | `false` | 是否启用 freshness-aware KG retrieval decay |

---

## 10. 剩余增强项

- [x] 多 worker / 多实例部署下的 scheduler 协调已实现（source lease + 可选 loop leader election）。
- [x] 通用 tool chaining / output piping 已通过 `ToolCallPlan.input_bindings` + `AgentCore` 执行器实现。
- [x] RSS / Feed source 已支持直接 feed URL 展开并抓取文章页。
- [x] D2 已下沉到 `lightrag_fork/operate.py` 的 KG 检索排序链路，retrieval tool 层仅保留兼容性 fallback。

---

## 11. 文档来源说明

- 当前仓库保留 `Dygraph.working.md` 作为工作区镜像文档。
- 桌面文件 `C:\Users\15770\Desktop\Dygraph.md` 已同步为同内容版本。
- 如后续继续迭代，以桌面文件和本工作区镜像同时维护。
