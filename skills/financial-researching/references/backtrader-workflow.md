# backtrader 工作流参考

本文件说明 backtrader 的核心组件职责和从研究结果到回测策略的转换路径。
仅在需要了解"backtrader 怎么组织"时查阅。

---

## 一、核心组件职责

### Cerebro（大脑）

- backtrader 的总控制器，负责组装和运行回测
- 添加数据源、策略、分析器、观察器
- 设置初始资金、手续费、滑点

```python
import backtrader as bt

cerebro = bt.Cerebro()
cerebro.broker.setcash(1_000_000)           # 初始资金
cerebro.broker.setcommission(commission=0.001)  # 手续费 0.1%
```

### Data Feed（数据馈送）

- 将行情数据注入 Cerebro
- 支持 CSV 文件或 Pandas DataFrame
- 必须包含 `datetime`、`open`、`high`、`low`、`close`、`volume` 列

```python
# 从 Pandas DataFrame 加载
data = bt.feeds.PandasData(dataname=df, datetime='date')
cerebro.adddata(data)
```

### Strategy（策略）

- 继承 `bt.Strategy`，包含交易逻辑
- `__init__`：初始化指标
- `next()`：每根K线执行一次，包含买卖判断

```python
class MyStrategy(bt.Strategy):
    def __init__(self):
        self.sma = bt.indicators.SimpleMovingAverage(self.data.close, period=20)

    def next(self):
        if self.data.close[0] > self.sma[0]:
            if not self.position:
                self.buy()
        elif self.data.close[0] < self.sma[0]:
            if self.position:
                self.sell()
```

### Indicator（指标）

- 技术指标计算引擎
- 内置常用指标：SMA、EMA、RSI、MACD、Bollinger 等
- 也可自定义指标（继承 `bt.Indicator`）

### Broker（经纪人）

- 模拟交易执行
- 管理现金、持仓、订单
- 可设置手续费、滑点、保证金

### Analyzer（分析器）

- 回测结束后计算绩效指标
- 内置分析器：SharpeRatio、DrawDown、TradeAnalyzer 等
- 通过 `cerebro.addanalyzer()` 添加

---

## 二、从研究结果到回测策略的转换路径

### 典型流程

```
面板回归结果
    │
    ├── 识别显著因子（p < 0.05）
    │
    ├── 确定因子方向（正/负系数）
    │
    ├── 设计交易规则：
    │   ├── 做多条件：因子值 > 阈值（正向因子）
    │   ├── 做空/平仓条件：因子值 < 阈值
    │   └── 持仓期限：根据研究频率决定
    │
    ├── 生成信号序列：
    │   └── DataFrame (date, code, signal)
    │       signal: 1 = 买入, -1 = 卖出, 0 = 持有
    │
    └── 喂入 backtrader 执行回测
```

### 信号接入方式

**方式一：策略内部计算信号**

在 Strategy 的 `__init__` 或 `next()` 中直接基于数据计算信号。

**方式二：外部信号文件（推荐）**

1. 将信号列合并到行情数据中
2. 使用自定义 Data Feed 读取信号列
3. Strategy 中直接读取信号值做判断

```python
class SignalData(bt.feeds.PandasData):
    lines = ('signal',)
    params = (('signal', -1),)  # -1 表示自动检测列位置

class SignalStrategy(bt.Strategy):
    def next(self):
        if self.data.signal[0] == 1 and not self.position:
            self.buy()
        elif self.data.signal[0] == -1 and self.position:
            self.sell()
```

---

## 三、回测注意事项

1. **避免未来函数**：策略中不要使用当前K线之后的数据
2. **复权价格**：必须使用前复权或后复权数据，不要用不复权数据
3. **手续费和滑点**：必须显式设置，否则结果不可信
4. **初始资金**：根据研究对象合理设定（A 股单只股票建议 100 万起）
5. **数据频率一致**：日线策略用日线数据，不要混用不同频率
6. **样本外验证**：用一段时间训练，另一段时间验证，避免过拟合
