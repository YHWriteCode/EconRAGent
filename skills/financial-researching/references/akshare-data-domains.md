# AKShare 数据域参考

本文件列出 financial-researching skill 首期支持的数据域及推荐接口。
仅在需要了解"有哪些数据可取、用哪个接口"时查阅此文件。

---

## 一、首期支持的数据域

| 数据域 | 说明 | 典型用途 |
|--------|------|---------|
| A 股日线行情 | 个股日K线（OHLCV） | 回测数据源、收益率计算 |
| A 股复权行情 | 前复权 / 后复权价格 | 策略回测（必须用复权价） |
| 个股基本面 | 市盈率、市净率、股息率等 | 因子研究、面板回归 |
| 行业分类 | 申万行业分类 | 行业固定效应、分组分析 |
| 宏观经济 | GDP、CPI、M2、利率 | 宏观因子、控制变量 |
| 指数行情 | 沪深300、中证500等指数日线 | 基准对比、Beta 计算 |

---

## 二、推荐接口映射

### A 股日线行情

```python
import akshare as ak

# 个股日线（不复权）
df = ak.stock_zh_a_hist(symbol="000001", period="daily",
                        start_date="20200101", end_date="20231231",
                        adjust="")

# 个股日线（前复权）— 回测时必须使用
df = ak.stock_zh_a_hist(symbol="000001", period="daily",
                        start_date="20200101", end_date="20231231",
                        adjust="qfq")
```

**注意**：`symbol` 参数为 6 位纯数字，不带市场前缀。

### 个股基本面指标

```python
# 个股实时市盈率等指标（可能需根据版本确认接口名）
# 待按环境调整：AKShare 版本更新可能导致接口名或参数变化
df = ak.stock_a_indicator_lg(symbol="000001")
```

### 行业分类

```python
# 申万行业分类成分股
df = ak.index_stock_cons_csindex(symbol="H30533")  # 示例：某行业指数
# 备选：ak.stock_board_industry_name_em() 获取行业板块列表
```

### 宏观经济数据

```python
# GDP 季度数据
df = ak.macro_china_gdp()

# CPI 月度数据
df = ak.macro_china_cpi()

# M2 货币供应
df = ak.macro_china_money_supply()
```

### 指数日线

```python
# 沪深300日线
df = ak.stock_zh_index_daily(symbol="sh000300")
```

---

## 三、接口变动风险

AKShare 是社区维护项目，接口名和返回列名可能随版本更新变化。应对策略：

1. **不要硬编码列名**：在数据获取后立即通过列名映射字典做转换
2. **捕获异常**：对接口调用用 `try/except` 包裹，失败时给出清晰错误信息
3. **版本锁定**：在 requirements 中锁定 AKShare 大版本号
4. **定期验证**：每次更新 AKShare 后回归测试数据获取脚本

---

## 四、暂不支持的数据域（后续扩展）

- 期货与期权行情
- 港股 / 美股行情
- 可转债数据
- 龙虎榜 / 大宗交易
- 分钟级别高频数据
- 另类数据（舆情、ESG）

这些数据域的接入路径与当前 skill 一致（AKShare → 标准化 → 面板），优先级由用户需求决定。
