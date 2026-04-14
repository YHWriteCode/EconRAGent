# linearmodels 模型选择指南

本文件帮助 agent 在面板回归任务中快速选择合适的模型。
仅在需要决定"用哪种回归模型"时查阅。

---

## 一、模型选择决策树

```
用户需要面板回归
│
├── 是否存在内生性问题（某自变量与误差项相关）？
│   ├── 是 → 使用 IV2SLS（工具变量 / 两阶段最小二乘）
│   └── 否 → 继续判断
│
├── 是否有充分理由认为个体效应与自变量相关？
│   ├── 是 → 使用 PanelOLS（固定效应）
│   ├── 否 → 使用 RandomEffects（随机效应）
│   └── 不确定 → 执行 Hausman 检验后决定
│
└── 是否主要关心变量的变化量（消除水平差异）？
    └── 是 → 使用 FirstDifferenceOLS
```

---

## 二、各模型适用场景

### PanelOLS（固定效应）

- **适用场景**：个体特征（公司文化、管理层能力等不可观测因素）可能与自变量相关
- **典型用法**：研究某因子对股票收益的影响，同时控制个股固定效应和时间固定效应
- **关键参数**：`entity_effects=True`、`time_effects=True`
- **注意**：固定效应会吸收不随时间变化的变量（如行业），这些变量的系数无法估计

```python
from linearmodels.panel import PanelOLS

model = PanelOLS(dependent=y, exog=X, entity_effects=True, time_effects=True)
result = model.fit(cov_type='clustered', cluster_entity=True)
```

### RandomEffects（随机效应）

- **适用场景**：个体效应与自变量不相关（Hausman 检验不拒绝原假设）
- **优势**：可估计不随时间变化的变量的系数
- **注意**：如果 Hausman 检验拒绝原假设，应改用固定效应

```python
from linearmodels.panel import RandomEffects

model = RandomEffects(dependent=y, exog=X)
result = model.fit(cov_type='clustered', cluster_entity=True)
```

### FirstDifferenceOLS（一阶差分）

- **适用场景**：仅有两期数据，或关注变量变化量而非水平值
- **优势**：简单有效地消除个体固定效应
- **限制**：牺牲一期观测，样本量减少

```python
from linearmodels.panel import FirstDifferenceOLS

model = FirstDifferenceOLS(dependent=y, exog=X)
result = model.fit(cov_type='robust')
```

### IV2SLS（工具变量）

- **适用场景**：核心自变量存在内生性（遗漏变量、反向因果、测量误差）
- **要求**：必须有合格的工具变量（与内生变量相关，但与误差项不相关）
- **诊断**：检查第一阶段 F 统计量（>10），做过度识别检验

```python
from linearmodels.iv import IV2SLS

model = IV2SLS(dependent=y, exog=X_exog, endog=X_endog, instruments=Z)
result = model.fit(cov_type='robust')
```

---

## 三、Hausman 检验速查

Hausman 检验用于在固定效应和随机效应之间做选择：

1. 分别估计 FE 和 RE 模型
2. 比较两组系数差异
3. 原假设：RE 一致（个体效应与自变量不相关）
4. 若 p < 0.05：拒绝原假设 → 使用固定效应
5. 若 p ≥ 0.05：不拒绝原假设 → 可使用随机效应

> linearmodels 目前没有内置 Hausman 检验函数，需手动计算或使用 statsmodels 辅助。

---

## 四、标准误选择

| 选择 | 参数 | 适用 |
|------|------|------|
| 聚类稳健（推荐默认） | `cov_type='clustered', cluster_entity=True` | 面板数据存在组内相关 |
| 异方差稳健 | `cov_type='robust'` | 截面数据或简单面板 |
| 内核稳健 | `cov_type='kernel'` | 时间序列相关较强 |

**默认规则**：面板数据一律使用聚类稳健标准误，除非有充分理由使用其他类型。
