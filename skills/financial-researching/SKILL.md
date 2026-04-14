---
name: financial-researching
description: >-
  该 skill 为 agent 提供金融数据获取、面板回归研究和策略回测的端到端能力。
  当用户需要拉取 A 股/基金/宏观等金融数据、执行面板回归或因子检验、
  或将研究结论转化为量化策略并回测时，应触发此 skill。
  覆盖"取数 → 清洗 → 建模 → 信号生成 → 回测 → 绩效评估"全链路。
version: 0.1.0
tags:
  - finance
  - quantitative-research
  - panel-regression
  - backtesting
scripts:
  - scripts/fetch_market_data.py
  - scripts/prepare_panel_data.py
  - scripts/run_panel_model.py
  - scripts/akshare_to_backtrader.py
  - scripts/run_backtest.py
  - scripts/fetch_model_backtest.py
references:
  - references/akshare-data-domains.md
  - references/data-schema-rules.md
  - references/linearmodels-model-selection.md
  - references/linearmodels-panel-regression.md
  - references/backtrader-workflow.md
  - references/backtrader-analyzers-and-metrics.md
---

# Financial Researching

本 skill 封装了金融研究的三大核心能力：**数据获取与清洗**（AKShare）、**面板回归与因子检验**（linearmodels）、**策略回测与绩效评估**（backtrader）。

---

## 一、适用任务判断

收到用户请求后，先判断属于以下哪种任务类型：

| 任务类型 | 关键词信号 | 首要动作 |
|----------|-----------|---------|
| **数据获取** | "拉取数据""获取行情""下载K线""取宏观数据" | 确认标的、频率、时间范围，调用 AKShare |
| **研究建模** | "面板回归""因子检验""显著性""固定效应""工具变量" | 确认因变量、自变量、面板结构，使用 linearmodels |
| **策略回测** | "回测""策略""买入卖出信号""绩效""最大回撤" | 确认数据源、策略规则、评估指标，使用 backtrader |
| **端到端流程** | "从取数到回测""完整研究""一条龙" | 按 取数→清洗→建模→信号→回测 顺序串联执行 |

**默认原则**：无论哪种任务，都优先完成数据定义与清洗。没有干净数据，后续步骤全部无效。

---

## 二、数据层：AKShare

### 使用时机

- 需要获取 A 股行情、财务指标、基金净值、宏观经济数据时
- 需要将外部数据标准化为统一 schema 时

### 数据标准化规则（核心）

所有从 AKShare 获取的数据必须经过以下标准化：

1. **日期列**：统一命名为 `date`，格式 `YYYY-MM-DD`，dtype 为 `datetime64[ns]`
2. **证券代码列**：统一命名为 `code`，6 位纯数字字符串（如 `"000001"`），不带市场前缀
3. **价格列**：`open`、`high`、`low`、`close`，浮点数，单位：元
4. **成交量列**：`volume`，整数，单位：股（非手）
5. **成交额列**：`amount`，浮点数，单位：元
6. **收益率列**：`return`，浮点数，小数形式（0.05 = 5%）
7. **缺失值**：价格列用前值填充（`ffill`），成交量缺失填 0，其余保留 NaN 并在建模前检查

> **注意**：AKShare 接口返回的列名经常变动。脚本中必须做列名映射，不要硬编码依赖原始列名。

### 关于 AKShare 接口细节

不要在此处逐一查阅接口列表。当需要了解具体数据域覆盖范围和推荐接口时：
→ 查阅 `references/akshare-data-domains.md`

当需要了解标准化表结构的完整规范时：
→ 查阅 `references/data-schema-rules.md`

### 推荐脚本

- `scripts/fetch_market_data.py`：获取行情数据并标准化输出
- `scripts/prepare_panel_data.py`：将多标的数据整合为面板格式

---

## 三、研究层：linearmodels

### 使用时机

- 用户需要面板回归（固定效应、随机效应、一阶差分）
- 用户需要因子显著性检验
- 用户需要工具变量 / 2SLS 估计
- 用户需要聚类稳健标准误

### 高层使用原则

1. **输入必须是面板格式**：DataFrame 必须设置 MultiIndex `(code, date)`
2. **因变量与自变量必须是数值型**，不接受分类变量作为回归变量（需提前 dummy 化）
3. **模型选择不要凭感觉**，先执行 Hausman 检验再决定 FE 还是 RE
4. **标准误默认使用聚类稳健标准误**（`cluster_entity=True`）
5. **结果必须报告**：系数、t 值、p 值、R²、F 统计量、观测数

### 关于模型选择和面板回归细节

→ 查阅 `references/linearmodels-model-selection.md`
→ 查阅 `references/linearmodels-panel-regression.md`

### 推荐脚本

- `scripts/run_panel_model.py`：执行标准面板回归并输出结果

---

## 四、回测层：backtrader

### 使用时机

- 用户需要将研究结论（如"某因子显著"）转化为可执行策略
- 用户需要模拟历史交易验证策略有效性
- 用户需要评估策略绩效指标（收益、回撤、Sharpe 等）

### 高层使用原则

1. **数据馈送格式严格**：必须包含 `date`、`open`、`high`、`low`、`close`、`volume` 列
2. **策略类继承 `bt.Strategy`**，所有买卖逻辑写在 `next()` 方法中
3. **不要在策略中做数据清洗**，数据清洗必须在喂入 backtrader 之前完成
4. **初始资金、手续费、滑点必须显式设置**，不要使用默认值而不声明
5. **回测结果必须输出**：总收益率、年化收益率、最大回撤、Sharpe 比率、交易次数

### 关于 backtrader 组件和评估指标

→ 查阅 `references/backtrader-workflow.md`
→ 查阅 `references/backtrader-analyzers-and-metrics.md`

### 推荐脚本

- `scripts/akshare_to_backtrader.py`：将 AKShare 数据转为 backtrader 数据馈送格式
- `scripts/run_backtest.py`：运行回测并输出绩效报告

---

## 五、端到端流程

当用户需要完整的"取数→建模→回测"流程时，使用：

- `scripts/fetch_model_backtest.py`：串联全链路的 MVP 脚本

该脚本内部按以下顺序执行：

```
1. fetch_data()       → 拉取数据并清洗
2. prepare_panel()    → 整理面板格式
3. run_model()        → 面板回归，提取显著因子
4. generate_signal()  → 将研究结论转为交易信号
5. run_backtest()     → 执行回测
6. report()           → 输出绩效摘要
```

---

## 六、输出产物

| 阶段 | 产物 | 格式 |
|------|------|------|
| 数据获取 | 标准化行情数据 | CSV / DataFrame |
| 面板整理 | 面板格式数据表 | CSV（MultiIndex） |
| 面板回归 | 回归结果摘要 | 文本 / DataFrame |
| 信号生成 | 交易信号序列 | CSV（date, code, signal） |
| 回测 | 绩效报告 | 文本 / JSON |

所有中间文件默认输出到工作目录下的 `output/` 子目录。

---

## 七、常见错误与防呆规则

1. **AKShare 返回空 DataFrame**：检查日期范围是否为交易日、代码是否正确、网络是否可用
2. **列名不匹配**：AKShare 接口升级后列名可能变更，始终通过列名映射字典处理，不要硬编码
3. **面板索引未设置**：linearmodels 要求 MultiIndex，传入普通 DataFrame 会报错
4. **backtrader 日期格式错误**：必须是 `datetime` 类型，字符串无法解析
5. **回测无交易**：检查策略逻辑是否正确触发了 `buy()`/`sell()`，检查数据是否覆盖了足够时间段
6. **内存溢出**：大规模面板数据应分批处理，避免一次性加载全 A 股多年数据
7. **NaN 传入回归**：linearmodels 遇到 NaN 会报错，必须在建模前 `dropna()`
