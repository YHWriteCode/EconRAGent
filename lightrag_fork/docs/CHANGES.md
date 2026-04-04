# 项目变更记录

## 一、原始 LightRAG 核心库

本项目基于原始 LightRAG 核心库进行定制开发，当前采用“尽量复用核心执行链路、按模块最小侵入式增强”的方式推进。本阶段未重写 LightRAG 的文档切分、实体关系抽取、图向量写入、检索生成等主流程，而是在其现有抽象上补充并发控制与领域 schema 能力。

原始核心文件职责如下：

- `lightrag.py`
  - 主编排入口。
  - 负责 LightRAG 初始化、存储初始化、文档插入、查询入口、配置下发。
- `operate.py`
  - 核心执行逻辑。
  - 负责 chunk 切分、实体/关系抽取、图谱合并、向量写入、检索模式实现等。
- `base.py`
  - 定义 KV / Vector / Graph / DocStatus 四类存储抽象接口。
- `kg/`
  - 各类后端存储实现，例如 Redis、Neo4j、Qdrant、MongoDB 等。
- `llm/`
  - 模型调用适配层，支持 OpenAI 兼容接口、本地模型适配等。
- `api/`
  - 服务端 API、路由、文档管理、后台任务与服务启动逻辑。

原始并发控制主要依赖 `kg/shared_storage.py` 中的本地/多进程共享锁机制，包括：

- `KeyedUnifiedLock`
  - 用于实体/关系级 keyed lock，避免单机并发写图冲突。
- `NamespaceLock`
  - 用于 namespace 级互斥，如 `pipeline_status` 保护。
- `initialize_pipeline_status`
  - 初始化 `busy / request_pending / cancellation_requested / history_messages` 等运行态。
- `get_data_init_lock` / `get_internal_lock`
  - 用于进程内初始化与共享变量保护。

该方案适合单机或同机多进程，但不能为多实例部署提供跨实例互斥保证。

## 二、本次改造概述

本阶段核心目标分为三部分：

1. 用 Redis 分布式锁增强原有本地锁方案，使 LightRAG 在多实例部署下具备更可靠的并发安全能力，并保留本地锁作为单机开发和降级选项。
2. 增加"可选领域 schema"配置层与 prompt 注入能力，在不重写抽取主流程的前提下，使领域知识约束能够按配置生效，并保持默认通用模式行为不变。
3. 为图节点和边补充时间元数据维护能力，并在查询阶段提供可选的 freshness-aware 排序，支持上层动态图谱调度与时效性判断。

## 三、新增文件清单

### 1. `kg/lock_backend.py`

- 文件路径：`lightrag_fork/kg/lock_backend.py`
- 文件职责：
  - 抽象统一锁接口，屏蔽本地锁与 Redis 锁实现差异。
- 主要类/函数：
  - `LockLease`
  - `LockBackend`
  - `LocalLockBackend`
  - `LockLostError`
- 依赖关系：
  - 被 `kg/shared_storage.py` 调用，用于构造统一的 backend 锁上下文。

### 2. `kg/redis_lock_backend.py`

- 文件路径：`lightrag_fork/kg/redis_lock_backend.py`
- 文件职责：
  - 提供 Redis 分布式锁实现、续期与释放逻辑。
- 主要类/函数：
  - `RedisLockManager`
  - `RedisLockBackend`
  - Redis Lua acquire / renew / release 脚本封装
- 依赖关系：
  - 由 `kg/shared_storage.py` 按配置选择使用。

### 3. `schema.py`

- 文件路径：`lightrag_fork/schema.py`
- 文件职责：
  - 统一定义领域 schema 配置结构、内置 schema 解析与运行时标准化逻辑。
- 主要类/函数：
  - `EntityTypeDefinition`
  - `RelationTypeDefinition`
  - `DomainSchema`
  - `get_builtin_schema_registry()`
  - `resolve_domain_schema(...)`
  - `normalize_addon_schema_config(...)`
- 依赖关系：
  - 由 `lightrag.py` 在初始化阶段调用，结果通过 `addon_params` 传入 `operate.py`。

### 4. `schemas/__init__.py`

- 文件路径：`lightrag_fork/schemas/__init__.py`
- 文件职责：
  - 导出内置 schema profile。

### 5. `schemas/general.py`

- 文件路径：`lightrag_fork/schemas/general.py`
- 文件职责：
  - 定义默认通用 schema，确保未启用领域模式时行为与原始 LightRAG 尽量一致。
- 主要对象：
  - `GENERAL_DOMAIN_SCHEMA`

### 6. `schemas/economy.py`

- 文件路径：`lightrag_fork/schemas/economy.py`
- 文件职责：
  - 定义经济领域 schema，作为可选 profile。
- 主要对象：
  - `ECONOMY_DOMAIN_SCHEMA`
- 默认经济领域实体类型：
  - `Company`
  - `Industry`
  - `Metric`
  - `Policy`
  - `Event`
  - `Asset`
  - `Institution`
  - `Country`

### 7. `tests/e2e/debug_energy_single.py`

- 文件路径：`lightrag_fork/tests/e2e/debug_energy_single.py`
- 文件职责：
  - 单条英文经济样本文本调试脚本，用于验证完整插入链路与图写入结果。

### 8. `tests/e2e/debug_bulk_six.py`

- 文件路径：`lightrag_fork/tests/e2e/debug_bulk_six.py`
- 文件职责：
  - 六条英文经济样本文本批量调试脚本，用于验证批量插入与图谱构建稳定性。

### 9. `tests/e2e/debug_schema_compare.py`

- 文件路径：`lightrag_fork/tests/e2e/debug_schema_compare.py`
- 文件职责：
  - 对比通用模式与经济 schema 模式下的实体抽取结果差异，验证 schema 注入是否真正影响抽取结果。

## 四、修改文件清单

### 1. `kg/shared_storage.py`

- 文件路径：`lightrag_fork/kg/shared_storage.py`
- 修改内容：
  - 引入统一 backend 工厂。
  - 支持 `local` / `redis` 两种锁后端。
  - 支持 `strict` / `fallback_local` 两种 Redis 故障策略。
  - 增加 pipeline 级锁 helper。
- 修改前行为：
  - 仅有本地/多进程共享锁方案，无法跨实例互斥。
- 修改后行为：
  - 可根据环境变量选择本地锁或 Redis 锁。
  - Redis 不可用时可按策略直接失败或降级本地锁。
- 向后兼容性：
  - 保持原有调用模式尽量不变，业务侧仍通过 helper 获取锁上下文。

### 2. `kg/lock_backend.py`

- 文件路径：`lightrag_fork/kg/lock_backend.py`
- 修改内容：
  - 本地锁后端语义对齐。
  - 修正 non-blocking 获取逻辑。
- 修改前行为：
  - `wait_timeout<=0` 时并非严格立即返回。
- 修改后行为：
  - 本地锁与 Redis 锁在 non-blocking 语义上保持一致。
- 向后兼容性：
  - 对调用方透明，仅修正行为一致性。

### 3. `lightrag.py`

- 文件路径：`lightrag_fork/lightrag.py`
- 修改内容：
  - 接入 lock backend 配置。
  - 将 `domain_schema` 纳入 `addon_params` 并在初始化阶段标准化。
- 修改前行为：
  - 只有原始 `language / entity_types` 配置。
- 修改后行为：
  - 保持原有配置兼容，同时支持：
    - `addon_params["domain_schema"]["enabled"]`
    - `addon_params["domain_schema"]["mode"]`
    - `addon_params["domain_schema"]["profile_name"]`
- 向后兼容性：
  - 默认行为不变。
  - 未传 `domain_schema` 时仍按原通用模式运行。

### 4. `operate.py`

- 文件路径：`lightrag_fork/operate.py`
- 修改内容：
  - 在 `extract_entities(...)` 中新增 schema prompt 追加逻辑。
  - 新增：
    - `_build_domain_schema_prompt_appendix(...)`
    - `_append_domain_schema_prompt_block(...)`
  - 在图 merge 路径中维护 `created_at / last_confirmed_at / confirmation_count` 时间元数据。
  - 新增 freshness-aware 重排序辅助函数：
    - `_freshness_decay_enabled(...)`
    - `_freshness_decay_days(...)`
    - `_freshness_weighted_score(...)`
    - `_sort_items_with_freshness_decay(...)`
  - 在 `kg_query()` 的 local/global/hybrid 各模式检索结果中应用可选的 freshness decay 重排序。
  - `metadata.freshness_decay_applied` 标记是否实际生效。
- 修改前行为：
  - prompt 完全由 `prompt.py` 静态模板和原始变量格式化得到。
- 修改后行为：
  - 当 `domain_schema.enabled=True` 时，在三处 extraction prompt 末尾追加领域约束块。
  - 当 `domain_schema=None` 或未启用时，prompt 与修改前完全一致。
- 向后兼容性：
  - 完全兼容。
  - 不改 `prompt.py` 模板，不改抽取主流程，不改图/向量存储结构。

### 5. `api/routers/document_routes.py`

- 文件路径：`lightrag_fork/api/routers/document_routes.py`
- 修改内容：
  - 接入 runtime lock。
  - 修复后台删除的 `request_pending` 读写顺序问题。
  - 修复锁获取后早期异常释放窗口。
- 修改前行为：
  - API 层并发门禁依赖本地运行态，且删除流程存在状态读写顺序问题。
- 修改后行为：
  - 删除、清空等关键写路径与 pipeline runtime 锁对齐。
- 向后兼容性：
  - 对外 API 不变。

### 6. `llm/openai.py`

- 文件路径：`lightrag_fork/llm/openai.py`
- 修改内容：
  - 为 OpenAI 兼容接口增加关键词提取降级策略。
- 修改前行为：
  - 某些兼容端点在 `response_format` 不支持时会失败。
- 修改后行为：
  - 遇到不支持的端点时，自动退回到兼容 JSON 输出的 prompt 路径。
- 向后兼容性：
  - 对 OpenAI 标准接口无破坏。

### 7. `tests/e2e/test_pipeline_e2e.py`

- 文件路径：`lightrag_fork/tests/e2e/test_pipeline_e2e.py`
- 修改内容：
  - 修正根目录 `.env` 读取。
  - 修正 LLM wrapper 装配。
  - 修正 embedding 配置同步。
  - 增加 `LLM_log.md` 输入输出日志落盘能力。
- 修改前行为：
  - `.env` 读取、模型装配和 embedding 元数据存在偏差，调试信息不足。
- 修改后行为：
  - E2E 测试脚本可在真实环境中更稳定运行，并可记录 LLM 调用原始输入输出。
- 向后兼容性：
  - 属于测试与调试增强，不影响主业务逻辑。

### 8. `constants.py`

- 文件路径：`lightrag_fork/constants.py`
- 修改内容：
  - 增加 domain schema 默认常量。
- 修改前行为：
  - 无显式 schema 默认配置。
- 修改后行为：
  - 为 schema 配置层提供默认行为基线。
- 向后兼容性：
  - 完全兼容。

## 五、新增环境变量汇总表

| 变量名 | 默认值 | 含义 | 推荐生产值 |
|---|---|---|---|
| `LIGHTRAG_LOCK_BACKEND` | `local` | 锁后端类型，可选 `local` / `redis` | `redis` |
| `LIGHTRAG_LOCK_FAIL_MODE` | `strict` | Redis 后端故障策略，可选 `strict` / `fallback_local` | `strict` |
| `LIGHTRAG_LOCK_KEY_PREFIX` | `lightrag:lock` | Redis 锁 key 前缀 | `lightrag:lock` |
| `LIGHTRAG_LOCK_RENEW_INTERVAL_S` | `None` | 自动续期固定间隔；为空时回退为 `ttl/3` | 根据任务时长调优 |
| `LIGHTRAG_PIPELINE_RUNTIME_LOCK_WAIT_TIMEOUT_S` | `0` | pipeline runtime 锁等待时间 | `0` 或小于 1 的短等待 |
| `LIGHTRAG_PIPELINE_ENQUEUE_LOCK_WAIT_TIMEOUT_S` | `None` | enqueue 锁等待时间 | 按吞吐要求设置 |
| `LIGHTRAG_LOCK_MAX_RETRIES` | `None` | 获取锁最大重试次数 | 按业务时延设置 |
| `LIGHTRAG_LOCK_LOST_CHECK_INTERVAL_S` | `0.5` | 持锁期间 `lease.lost` 检查周期 | `0.5` |

说明：

- `LIGHTRAG_LOCK_BACKEND=local`
  - 仅使用本地锁，不依赖 Redis。
- `LIGHTRAG_LOCK_BACKEND=redis`
  - 正常使用 Redis 分布式锁。
- `LIGHTRAG_LOCK_FAIL_MODE=strict`
  - Redis 不可用时直接失败。
- `LIGHTRAG_LOCK_FAIL_MODE=fallback_local`
  - Redis 不可用时降级为本地锁，仅适用于开发或容灾保活，不提供强一致性。

## 六、锁分层结构说明

```text
LightRAG / API
   |
   v
shared_storage.py helpers
   |- get_pipeline_runtime_lock(...)
   |- get_pipeline_enqueue_lock(...)
   |- get_storage_keyed_lock(...)
   |
   v
LockBackend (abstract)
   |- LocalLockBackend
   \- RedisLockBackend
         |
         \- RedisLockManager
```

运行关系：

- 业务代码不直接感知 Redis Lua、续期任务、token 校验。
- 所有锁获取、续期、释放统一由 backend 处理。
- `shared_storage.py` 继续作为原有业务层与锁实现之间的边界层。

## 七、关键并发保护点说明

### 1. 文档入队（enqueue）

- 保护锁：
  - `pipeline enqueue lock`
- key 规则：
  - `lightrag:lock:{workspace}:pipeline:enqueue`
- 作用：
  - 保护 `filter_keys -> full_docs.upsert -> doc_status.upsert` 关键区段。

### 2. 文档处理（pipeline runtime）

- 保护锁：
  - `pipeline runtime lock`
- key 规则：
  - `lightrag:lock:{workspace}:pipeline:runtime`
- 作用：
  - 保证同一 workspace 下同时只有一个 pipeline 写路径在运行。

### 3. 图节点/关系合并（keyed lock）

- 保护锁：
  - `storage keyed lock`
- key 规则：
  - 实体：`lightrag:lock:{workspace}:GraphDB:{entity}`
  - 关系：`lightrag:lock:{workspace}:GraphDB:{src}:{tgt}`
- 作用：
  - 避免并发图合并时出现重复写入或冲突。

### 4. 文档删除

- 保护锁：
  - 与 pipeline 共用 `runtime lock`
- 作用：
  - 保证删除与写入处理互斥。

### 5. 批量清空

- 保护锁：
  - 与 pipeline 共用 `runtime lock`
- 作用：
  - 保证清空操作不会与插入/删除流程并发冲突。

## 八、领域 schema 模块化设计

### 1. 配置入口

本项目没有新造一套完全独立的配置体系，而是复用原有 `addon_params` 作为 schema 配置入口：

```python
addon_params = {
    "language": "English",
    "entity_types": [...],
    "domain_schema": {
        "enabled": False,
        "mode": "general",
        "profile_name": "general",
    },
}
```

### 2. 标准化后的 schema 结构

`schema.py` 负责把外部输入标准化为统一结构。核心字段包括：

- `enabled`
- `mode`
- `profile_name`
- `domain_name`
- `entity_types`
- `entity_type_names`
- `relation_types`
- `relation_type_names`
- `aliases`
- `extraction_rules`
- `metadata`

### 3. 内置 schema

当前内置两个 profile：

- `general`
  - 默认通用 schema。
  - 尽量保持与原始 LightRAG 默认抽取语义一致。
- `economy`
  - 经济领域 schema。
  - 内置实体类型、关系类型、别名与抽取约束。

### 4. 经济领域默认实体类型

- `Company`
- `Industry`
- `Metric`
- `Policy`
- `Event`
- `Asset`
- `Institution`
- `Country`

## 九、schema prompt 注入设计

### 1. 注入原则

- 不重写 LightRAG 原始抽取流程。
- 不改 `prompt.py` 静态模板内容。
- 采用“固定主模板 + 可选 schema 追加块”的方式。
- schema 只作为引导，不作为强制白名单过滤。

### 2. 注入位置

注入逻辑仅位于：

- `lightrag_fork/operate.py`
  - `extract_entities(...)`

LightRAG 当前实体与关系抽取共用同一套 extraction prompt，因此没有单独的关系 prompt 组装函数需要修改。

### 3. 追加块格式

当 `domain_schema.enabled=True` 且存在有效字段时，在 prompt 末尾追加：

```text
---
[领域约束]
本次抽取优先关注以下实体类型：{entity_types}
本次抽取优先关注以下关系类型：{relation_types}
抽取约束：{extraction_rules}
注意：以上为优先引导，不要遗漏文本中显著的通用实体。
---
```

字段缺失时直接跳过对应行，不报错。

### 4. 注入到的 prompt

追加块会被加到三处 prompt 上：

- `entity_extraction_system_prompt`
- `entity_extraction_user_prompt`
- `entity_continue_extraction_user_prompt`

### 5. 参数传递链路

参数链路为显式传递，不使用全局变量：

1. `LightRAG(addon_params=...)`
2. `lightrag.py::__post_init__()`
3. `schema.py::normalize_addon_schema_config(...)`
4. `global_config["addon_params"]["domain_schema"]`
5. `operate.py::extract_entities(...)`

### 6. 向后兼容性

当 `domain_schema=None`、未传、非字典或 `enabled=False` 时：

- 不追加任何 schema block
- 最终 prompt 与改动前保持一致

## 十、schema 最小验证结果

### 1. 验证脚本

- 文件路径：`lightrag_fork/tests/e2e/debug_schema_compare.py`
- 作用：
  - 用同一批英文经济文本，对比 `general` 与 `economy` 两种模式下的实体抽取差异。

### 2. 样本文本覆盖内容

脚本内置 3 段英文经济文本，覆盖：

- 公司名称
- 政策表述
- 行业/指标
- 资产/国家

### 3. 验证方法

分别使用两种模式插入同一批文档：

- 通用模式：
  - `domain_schema.enabled=False`
  - `profile_name="general"`
- 经济领域模式：
  - `domain_schema.enabled=True`
  - `profile_name="economy"`

然后读取图中最终实体与实体类型，对比两种模式下的类型变化。

### 4. 实际结果

脚本已在本地 `.venv` 真实运行通过，退出码为 `0`，输出结果表明 schema 注入已实际影响抽取结果。

通用模式类型统计：

- `organization: 7`
- `concept: 7`
- `location: 3`
- `naturalobject: 2`
- `data: 1`

经济领域模式类型统计：

- `company: 3`
- `industry: 3`
- `policy: 3`
- `metric: 9`
- `asset: 3`
- `institution: 1`
- `country: 3`

典型类型迁移如下：

- `BYD Co.` / `Exxon Mobil` / `JPMorgan Chase`
  - `organization -> company`
- `Electric Vehicle Industry` / `Banking Industry` / `Global Energy Industry`
  - `organization -> industry`
- `Green Manufacturing Subsidy And Tax Rebate Policy`
  - `concept -> policy`
- `Federal Reserve`
  - `organization -> institution`
- `China` / `United States` / `Saudi Arabia`
  - `location -> country`
- `Brent Crude` / `Lithium Carbonate`
  - `naturalobject -> asset`
- `Gross Margin` / `Revenue Growth` / `Bank Net Interest Margin` / `Credit Demand` / `Inflation Indicators`
  - `concept/data -> metric`

结果文件：

- `lightrag_fork/tests/e2e/schema_compare_output.json`

结论：

- schema 注入不会替换原抽取流程；
- 但会显著影响 LLM 对同一文本的实体类型理解与归类偏好；
- 这证明当前方案已经具备“通用模式 / 领域模式可切换”的实际能力。

## 十一、测试与验收状态

截至当前阶段，已完成的关键验证包括：

### 1. 分布式锁与并发控制

- `redis_lock`：通过
- `graph_vector`：通过
- `concurrent`：通过
- `fallback_local`：通过

说明：

- `Redis` 与 `local` 双后端可选
- `strict` 与 `fallback_local` 策略可选
- 同 workspace 并发互斥、删除与处理互斥均已在真实环境验证通过

### 2. 图与向量链路

- `Neo4j` 图节点写入：通过
- `Qdrant entities_vdb / chunks_vdb` 写入与查询：通过
- `MongoDocStatusStorage`：通过
- `naive / hybrid` 查询：通过

### 3. schema 功能验证

- `domain_schema=None`
  - prompt 与改动前一致：通过
- `domain_schema=economy_schema`
  - 三处 prompt 均出现领域约束块：通过
- 缺字段不报错：通过
- 参数传递链路显式、非全局变量：通过
- schema 对抽取结果产生实际影响：通过

### 4. 动态图谱与 freshness 功能验证

- 图 merge 时时间元数据正确初始化与更新：通过
- `convert_to_user_format()` 透传 `created_at / last_confirmed_at / confirmation_count / rank`：通过
- `enable_freshness_decay=True` 时重排序实际生效：通过
- `enable_freshness_decay=False`（默认）时行为与改动前一致：通过
- `metadata.freshness_decay_applied` 标记正确：通过

## 十二、已知风险与局限性

### 1. 分布式锁仍存在的工程风险

- `fallback_local` 只适合开发或降级保活，不提供强一致性。
- Redis 主从切换、网络分区、瞬时不可用仍可能带来锁窗口风险。
- TTL 与任务时长耦合，若任务过长且续期抖动，仍存在误失锁风险。
- 删除 / 插入跨多存储后端缺乏全局事务，当前仍采用最终一致性思路。

### 2. schema 当前生效边界

- 当前 schema 只作用于抽取 prompt 的“引导层”。
- 还没有把 schema 深入到：
  - 图存储结构设计
  - 向量存储结构设计
  - 查询理解与答案生成阶段

### 3. 关系类型约束仍然较弱

- 当前 `relation_types` 主要通过 prompt 引导生效。
- 还没有在后处理或存储层做更强的关系规范化约束。

### 4. freshness-aware 排序仍为 v1 启发式方案

- 当前 freshness decay 排序基于半衰期启发式，叠加在现有排序信号上，而非完整的排序模型重构。
- `confirmation_count` 语义为累计确认次数，尚无基于独立来源数的确认统计。
- 排序仅影响 KG 检索阶段的实体/关系列表排序，不影响最终 LLM 生成答案的 context 组装顺序。

## 十三、当前阶段结论

截至目前，LightRAG 核心库已经完成三项关键增强：

1. 并发控制从"仅本地共享锁"升级为"本地锁 / Redis 锁可选"，并支持生产严格模式与开发降级模式。
2. 抽取配置从"仅通用实体类型"升级为"默认通用 schema + 可选经济领域 schema"，并通过最小 prompt 注入方式实现模块化扩展。
3. 动态图谱时间元数据维护与 freshness-aware 检索排序：图节点/边写入时维护 `created_at / last_confirmed_at / confirmation_count`，查询时支持可选的 freshness decay 重排序，并将时效性元数据透传至上层。

当前方案满足以下目标：

- 默认通用模式行为保持稳定；
- 经济领域 schema 可按配置启用；
- 不重写 LightRAG 主抽取流程；
- 不破坏图存储与向量存储结构；
- 查询时可按需启用 freshness-aware 排序；
- 已通过最小真实环境验证；
- 便于后续在毕业设计中解释“领域 schema 模块化设计”和“动态知识图谱增强 RAG”的实现路径。

## 十四、尚未覆盖的功能边界

本阶段尚未实现或未纳入本轮改造范围的内容包括：

- agent 工具调用编排
- 历史记忆检索
- 新闻抓取
- 量化交易接口
- schema-aware 查询增强（schema 约束深入到检索与答案生成阶段）
- 完整的排序模型重构（当前 freshness decay 为 v1 启发式叠加）
- 前端展示层

这些内容应作为后续阶段独立设计，不建议与当前核心库稳定性改造混合推进。

## 十五、动态图谱相关补充变更（2026-04）

随着上层 `kg_agent` 引入动态图谱调度、纠错刷新和 freshness-aware 检索，`lightrag_fork` 在本轮补充了“时间元数据写入与查询透传”能力，仍然保持最小侵入式原则，不引入任何 agent/scheduler/memory 业务逻辑。

### 1. 变更目标

为图节点和边补充最小必要的时效性字段，供上层用于：

- 判断图谱结果是否过时
- 区分“图谱为空 / 图谱陈旧 / 图谱新鲜”
- 在检索后处理阶段尝试 freshness decay
- 将时效信息直接返回给 API 与 Agent 层

### 2. 本轮核心改动

#### 2.1 `operate.py`：节点/边时间元数据维护

在图 merge 与 rebuild 相关路径中补充并统一维护：

- `created_at`
- `last_confirmed_at`
- `confirmation_count`

行为约定如下：

- 新建节点/边时：
  - 初始化 `created_at`
  - 初始化 `last_confirmed_at`
  - 初始化 `confirmation_count = 1`
- 合并已有节点/边时：
  - 保留原始 `created_at`
  - 更新 `last_confirmed_at`
  - 递增 `confirmation_count`
- rebuild 关系时若发现端点节点缺失，补建节点时也同步初始化上述字段

#### 2.2 `utils.py`：查询结果透传

`convert_to_user_format()` 现在会把以下字段透传到用户态结构：

- `entities[].created_at`
- `entities[].last_confirmed_at`
- `entities[].confirmation_count`
- `entities[].rank`
- `relationships[].created_at`
- `relationships[].last_confirmed_at`
- `relationships[].confirmation_count`
- `relationships[].rank`

老数据若缺失这些字段，保持兼容并返回 `None` 或缺省值，不抛错。

#### 2.3 `base.py`：`QueryParam` 新增 freshness 查询参数

`QueryParam` 新增两个可选字段：

- `enable_freshness_decay: bool = False`
  - 是否在 KG 检索流程中启用 freshness-aware 重排序。
- `staleness_decay_days: float = 7.0`
  - freshness 衰减半衰期（天），控制时效性权重衰减速度。

#### 2.4 `operate.py`：freshness-aware 重排序

新增以下辅助函数：

- `_freshness_decay_enabled(query_param)` — 判断是否启用 freshness decay
- `_freshness_decay_days(query_param)` — 读取半衰期天数
- `_freshness_weighted_score(base_rank, last_confirmed_at, decay_days)` — 计算融合时效性的加权分数
- `_sort_items_with_freshness_decay(items, query_param, ...)` — 对实体/关系列表按 freshness 加权重排序

在 `kg_query()` 的 local/global/hybrid 各模式中，当 `enable_freshness_decay=True` 时：

- 对 `local_entities`、`local_relations`、`global_entities`、`global_relations` 分别应用 freshness 重排序
- 结果中 `metadata.freshness_decay_applied` 标记是否实际生效

加权公式：

```
freshness_score = 0.5 ^ (age_days / decay_days)
weighted_rank = base_rank * (0.3 + 0.7 * freshness_score)
```

`naive_query()` 中同样会将 `freshness_decay_applied` 标记写入 `metadata`。

### 3. 当前语义约定

本轮固定：

- `confirmation_count` 表示"累计确认事件次数"
- freshness-aware ranking 为 v1 启发式方案，在现有排序信号上叠加时效性衰减，而非完整的排序模型重构

本轮尚未实现：

- 基于"独立来源数"的确认统计

### 4. 与上层动态图谱模块的配合边界

本轮 `lightrag_fork` 仅提供底层时间元数据与结果透传，不承担：

- scheduler
- source/state 持久化
- 对话纠错刷新
- `web_search -> kg_ingest` bridge
- conversation memory / tool call history

这些逻辑均继续保留在 `kg_agent/`。

### 5. 当前状态结论

到本轮为止，`lightrag_fork` 已满足动态图谱 v1 对底层的要求：

- 图谱写入时保留和更新时间元数据
- 查询时可向上层返回时间元数据与 `rank`
- 查询时可通过 `QueryParam.enable_freshness_decay` 启用 freshness-aware 重排序
- `operate.py` 在 KG 检索流程中已实现基于半衰期的时效性加权排序（v1 启发式方案）
- `metadata.freshness_decay_applied` 可供上层判断是否实际应用了 freshness 衰减
- 不破坏既有主流程与调用接口

后续可增强方向包括：完整的排序模型重构、基于独立来源数的确认统计等。
