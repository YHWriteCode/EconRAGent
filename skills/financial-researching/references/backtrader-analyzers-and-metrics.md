# backtrader 分析器与绩效指标参考

本文件说明回测绩效评估中常用的指标及其在 backtrader 中的实现方式。
仅在需要了解"如何评估回测结果"时查阅。

---

## 一、核心绩效指标

| 指标 | 含义 | 计算口径 | 参考标准 |
|------|------|---------|---------|
| 总收益率 | 期末净值 / 期初净值 - 1 | 包含已实现和未实现盈亏 | > 0 为正收益 |
| 年化收益率 | 总收益率按年折算 | `(1 + total_return) ^ (252/交易日数) - 1` | > 无风险利率 |
| 最大回撤 | 净值从峰值到谷值的最大跌幅 | `max((peak - trough) / peak)` | < 20% 较好 |
| Sharpe 比率 | 风险调整后收益 | `(年化收益 - 无风险利率) / 年化波动率` | > 1 可接受，> 2 优秀 |
| 交易次数 | 完成的买入+卖出配对数 | — | 太少说明策略不活跃 |
| 胜率 | 盈利交易占比 | 盈利次数 / 总交易次数 | > 50% 为好 |
| 盈亏比 | 平均盈利 / 平均亏损 | — | > 1.5 较好 |
| 最长回撤期 | 净值从峰值到恢复的最长天数 | — | 越短越好 |

---

## 二、backtrader 内置分析器

### SharpeRatio

```python
cerebro.addanalyzer(bt.analyzers.SharpeRatio,
                    _name='sharpe',
                    riskfreerate=0.03,    # 年化无风险利率
                    annualize=True,
                    timeframe=bt.TimeFrame.Days)
```

提取方式：
```python
sharpe = results[0].analyzers.sharpe.get_analysis()
print(f"Sharpe: {sharpe['sharperatio']}")
```

### DrawDown

```python
cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
```

提取方式：
```python
dd = results[0].analyzers.drawdown.get_analysis()
print(f"最大回撤: {dd['max']['drawdown']:.2f}%")
print(f"最长回撤期: {dd['max']['len']} 天")
```

### TradeAnalyzer

```python
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
```

提取方式：
```python
ta = results[0].analyzers.trades.get_analysis()
total = ta.get('total', {}).get('total', 0)
won = ta.get('won', {}).get('total', 0)
lost = ta.get('lost', {}).get('total', 0)
win_rate = won / total if total > 0 else 0
print(f"交易次数: {total}, 胜率: {win_rate:.2%}")
```

### Returns

```python
cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
```

提取方式：
```python
ret = results[0].analyzers.returns.get_analysis()
print(f"总收益率: {ret['rtot']:.4f}")
print(f"年化收益率: {ret['rnorm']:.4f}")
```

---

## 三、推荐的标准分析器组合

以下组合覆盖了最常用的绩效维度，建议每次回测都添加：

```python
cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                    riskfreerate=0.03, annualize=True)
cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
```

---

## 四、绩效报告输出模板

```python
def print_performance(results):
    """从回测结果中提取并打印标准绩效报告"""
    strat = results[0]
    
    # 收益
    ret = strat.analyzers.returns.get_analysis()
    total_return = ret.get('rtot', 0)
    annual_return = ret.get('rnorm', 0)
    
    # Sharpe
    sharpe = strat.analyzers.sharpe.get_analysis()
    sharpe_ratio = sharpe.get('sharperatio', None)
    
    # 回撤
    dd = strat.analyzers.drawdown.get_analysis()
    max_dd = dd.get('max', {}).get('drawdown', 0)
    max_dd_len = dd.get('max', {}).get('len', 0)
    
    # 交易
    ta = strat.analyzers.trades.get_analysis()
    total_trades = ta.get('total', {}).get('total', 0)
    won = ta.get('won', {}).get('total', 0)
    win_rate = won / total_trades if total_trades > 0 else 0
    
    print("=" * 50)
    print("回测绩效报告")
    print("=" * 50)
    print(f"总收益率:     {total_return:.4%}")
    print(f"年化收益率:   {annual_return:.4%}")
    print(f"Sharpe 比率:  {sharpe_ratio:.4f}" if sharpe_ratio else "Sharpe 比率:  N/A")
    print(f"最大回撤:     {max_dd:.2f}%")
    print(f"最长回撤期:   {max_dd_len} 天")
    print(f"交易次数:     {total_trades}")
    print(f"胜率:         {win_rate:.2%}")
    print("=" * 50)
```

---

## 五、结果解读注意事项

1. **Sharpe 比率为负**：策略收益低于无风险利率，不可取
2. **交易次数为 0**：策略从未触发买卖信号，检查条件逻辑
3. **胜率高但总收益低**：赢小亏大，需关注盈亏比
4. **回测在牛市表现好**：不能说明策略有效，需在不同市场环境下验证
5. **年化收益 > 50%**：大概率过拟合，需样本外检验
