# linearmodels 面板回归操作参考

本文件说明面板回归的输入格式要求、执行步骤和输出解读。
仅在需要了解"面板回归具体怎么做"时查阅。

---

## 一、面板索引格式

linearmodels 要求 DataFrame 设置 MultiIndex，格式为 `(entity, time)`。

### 设置方式

```python
import pandas as pd

# 假设 df 已有 code 和 date 列
df['date'] = pd.to_datetime(df['date'])
df = df.set_index(['code', 'date'])
df = df.sort_index()
```

### 验证方式

```python
assert isinstance(df.index, pd.MultiIndex), "必须设置 MultiIndex"
assert df.index.names == ['code', 'date'], "索引名必须是 ['code', 'date']"
assert not df.index.duplicated().any(), "不允许重复索引"
```

---

## 二、典型输入表结构

```
                      return    pe      pb    market_cap   size_factor
code   date
000001 2023-01-03     0.012    15.3    1.20   3.50e+11     26.58
000001 2023-01-04    -0.003    15.1    1.19   3.48e+11     26.57
000002 2023-01-03     0.008    22.1    2.30   1.20e+11     25.51
000002 2023-01-04     0.015    22.5    2.35   1.22e+11     25.53
```

- **因变量**（y）：通常是 `return`（收益率）
- **自变量**（X）：因子列，如 `pe`、`pb`、`size_factor` 等
- 所有列必须是数值型（float 或 int）

---

## 三、执行面板回归的标准步骤

```python
import pandas as pd
from linearmodels.panel import PanelOLS
import statsmodels.api as sm

# 1. 读取并设置索引
df = pd.read_csv("panel_data.csv")
df['date'] = pd.to_datetime(df['date'])
df = df.set_index(['code', 'date']).sort_index()

# 2. 定义变量
y = df['return']
X = df[['pe', 'pb', 'size_factor']]
X = sm.add_constant(X)  # 添加截距项（如果不使用固定效应截距）

# 3. 构建模型
model = PanelOLS(dependent=y, exog=X, entity_effects=True)

# 4. 拟合（聚类稳健标准误）
result = model.fit(cov_type='clustered', cluster_entity=True)

# 5. 查看结果
print(result.summary)
```

---

## 四、回归结果解读要点

`result.summary` 输出中需要关注的关键信息：

| 字段 | 含义 | 判断标准 |
|------|------|---------|
| `coef` | 回归系数 | 经济意义是否合理 |
| `std err` | 标准误 | 越小越好 |
| `t-stat` | t 统计量 | 绝对值 > 2 通常显著 |
| `p-value` | p 值 | < 0.05 为显著，< 0.01 为高度显著 |
| `R-squared (within)` | 组内 R² | 固定效应模型的解释力 |
| `R-squared (between)` | 组间 R² | 截面方向的解释力 |
| `R-squared (overall)` | 总体 R² | 整体拟合度 |
| `F-statistic` | F 检验 | p < 0.05 表示模型整体显著 |
| `Entities / Time` | 个体数 / 时间数 | 面板维度信息 |

---

## 五、常见问题排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `ValueError: NaN` | 数据含缺失值 | 建模前 `df.dropna(subset=回归列)` |
| 系数全部不显著 | 因子与因变量无关 / 多重共线性 | 检查变量相关性矩阵、VIF |
| R² 极低 | 模型解释力弱 | 正常现象（金融数据R²普遍低），关注系数显著性 |
| 索引错误 | 未设置 MultiIndex | 检查 `df.set_index(['code', 'date'])` |
| 内存错误 | 面板太大 | 缩小时间范围或股票池 |

---

## 六、输出保存

```python
# 保存回归结果摘要为文本
with open("output/regression_summary.txt", "w", encoding="utf-8") as f:
    f.write(str(result.summary))

# 保存系数表为 CSV
coef_df = pd.DataFrame({
    'coef': result.params,
    'std_err': result.std_errors,
    't_stat': result.tstats,
    'p_value': result.pvalues,
})
coef_df.to_csv("output/coefficients.csv", encoding="utf-8")
```
