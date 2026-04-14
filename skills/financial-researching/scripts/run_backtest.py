"""
run_backtest.py
===============
职责：定义一个最小可运行的 backtrader 策略，执行回测并输出绩效报告。

用法：
    python run_backtest.py --input output/bt_000001.csv --cash 1000000 --commission 0.001 --output output/backtest_report.txt

依赖：pandas, backtrader
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

try:
    import backtrader as bt
except ImportError:
    print("错误：未安装 backtrader，请执行 pip install backtrader")
    sys.exit(1)


# ──────────────────────────────────────────────
# 自定义 Data Feed（支持信号列）
# ──────────────────────────────────────────────
class SignalPandasData(bt.feeds.PandasData):
    """扩展 PandasData，支持读取 signal 列"""
    lines = ("signal",)
    params = (("signal", -1),)  # -1 表示自动检测列


# ──────────────────────────────────────────────
# 均线策略（默认演示策略）
# ──────────────────────────────────────────────
class SMAStrategy(bt.Strategy):
    """
    简单均线策略：
    - 短期均线上穿长期均线 → 买入
    - 短期均线下穿长期均线 → 卖出
    """
    params = (
        ("short_period", 5),
        ("long_period", 20),
    )

    def __init__(self):
        self.sma_short = bt.indicators.SimpleMovingAverage(
            self.data.close, period=self.params.short_period
        )
        self.sma_long = bt.indicators.SimpleMovingAverage(
            self.data.close, period=self.params.long_period
        )
        self.crossover = bt.indicators.CrossOver(self.sma_short, self.sma_long)
        self.order = None

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                print(f"  买入执行: 价格={order.executed.price:.2f}, "
                      f"数量={order.executed.size}, "
                      f"手续费={order.executed.comm:.2f}")
            elif order.issell():
                print(f"  卖出执行: 价格={order.executed.price:.2f}, "
                      f"数量={order.executed.size}, "
                      f"手续费={order.executed.comm:.2f}")
        self.order = None

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.crossover > 0:
                self.order = self.buy()
        else:
            if self.crossover < 0:
                self.order = self.sell()


# ──────────────────────────────────────────────
# 外部信号策略
# ──────────────────────────────────────────────
class ExternalSignalStrategy(bt.Strategy):
    """
    读取外部信号的策略：
    - signal == 1 → 买入
    - signal == -1 → 卖出
    """

    def __init__(self):
        self.signal = self.data.signal
        self.order = None

    def notify_order(self, order):
        if order.status in [order.Completed]:
            action = "买入" if order.isbuy() else "卖出"
            print(f"  {action}执行: 价格={order.executed.price:.2f}, "
                  f"数量={order.executed.size}")
        self.order = None

    def next(self):
        if self.order:
            return

        if not self.position and self.signal[0] == 1:
            self.order = self.buy()
        elif self.position and self.signal[0] == -1:
            self.order = self.sell()


def load_data(filepath: str) -> pd.DataFrame:
    """加载 backtrader 格式的数据"""
    if not os.path.exists(filepath):
        print(f"错误：文件不存在 - {filepath}")
        sys.exit(1)

    df = pd.read_csv(filepath, parse_dates=["date"])

    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"错误：数据缺少必要列 - {missing}")
        sys.exit(1)

    df = df.sort_values("date").reset_index(drop=True)
    print(f"数据已加载：{len(df)} 行, {df['date'].min()} ~ {df['date'].max()}")
    return df


def print_performance(results, initial_cash: float):
    """提取并打印标准绩效报告"""
    strat = results[0]
    final_value = strat.broker.getvalue()
    total_return = (final_value - initial_cash) / initial_cash

    print("\n" + "=" * 55)
    print("               回 测 绩 效 报 告")
    print("=" * 55)
    print(f"  初始资金:       {initial_cash:>15,.2f} 元")
    print(f"  期末净值:       {final_value:>15,.2f} 元")
    print(f"  总收益率:       {total_return:>14.4%}")

    # Returns 分析器
    try:
        ret = strat.analyzers.returns.get_analysis()
        annual_return = ret.get("rnorm", 0)
        print(f"  年化收益率:     {annual_return:>14.4%}")
    except Exception:
        print("  年化收益率:            N/A")

    # Sharpe 分析器
    try:
        sharpe = strat.analyzers.sharpe.get_analysis()
        sharpe_ratio = sharpe.get("sharperatio", None)
        if sharpe_ratio is not None:
            print(f"  Sharpe 比率:    {sharpe_ratio:>14.4f}")
        else:
            print("  Sharpe 比率:           N/A")
    except Exception:
        print("  Sharpe 比率:           N/A")

    # DrawDown 分析器
    try:
        dd = strat.analyzers.drawdown.get_analysis()
        max_dd = dd.get("max", {}).get("drawdown", 0)
        max_dd_len = dd.get("max", {}).get("len", 0)
        print(f"  最大回撤:       {max_dd:>13.2f}%")
        print(f"  最长回撤期:     {max_dd_len:>12d} 天")
    except Exception:
        print("  最大回撤:              N/A")

    # TradeAnalyzer 分析器
    try:
        ta = strat.analyzers.trades.get_analysis()
        total_trades = ta.get("total", {}).get("total", 0)
        won = ta.get("won", {}).get("total", 0)
        lost = ta.get("lost", {}).get("total", 0)
        win_rate = won / total_trades if total_trades > 0 else 0
        print(f"  交易次数:       {total_trades:>12d}")
        print(f"  胜率:           {win_rate:>14.2%}")
    except Exception:
        print("  交易次数:              N/A")

    print("=" * 55)

    return {
        "initial_cash": initial_cash,
        "final_value": final_value,
        "total_return": total_return,
    }


def main():
    parser = argparse.ArgumentParser(description="运行 backtrader 回测")
    parser.add_argument("--input", type=str, required=True,
                        help="backtrader 格式数据 CSV 路径")
    parser.add_argument("--cash", type=float, default=1_000_000,
                        help="初始资金（元），默认 1000000")
    parser.add_argument("--commission", type=float, default=0.001,
                        help="手续费率，默认 0.001 (0.1%%)")
    parser.add_argument("--strategy", type=str, default="sma",
                        choices=["sma", "signal"],
                        help="策略类型：sma(均线), signal(外部信号)，默认 sma")
    parser.add_argument("--short-period", type=int, default=5,
                        help="短期均线周期（仅 sma 策略），默认 5")
    parser.add_argument("--long-period", type=int, default=20,
                        help="长期均线周期（仅 sma 策略），默认 20")
    parser.add_argument("--output", type=str, default="output/backtest_report.txt",
                        help="绩效报告输出路径")
    args = parser.parse_args()

    # 加载数据
    df = load_data(args.input)

    # 初始化 Cerebro
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(args.cash)
    cerebro.broker.setcommission(commission=args.commission)

    # 判断是否使用信号策略
    has_signal = "signal" in df.columns and args.strategy == "signal"

    # 添加数据馈送
    if has_signal:
        data = SignalPandasData(dataname=df, datetime="date")
        cerebro.addstrategy(ExternalSignalStrategy)
        print("使用策略：外部信号策略")
    else:
        data = bt.feeds.PandasData(dataname=df, datetime="date")
        cerebro.addstrategy(SMAStrategy,
                            short_period=args.short_period,
                            long_period=args.long_period)
        print(f"使用策略：SMA 均线策略 (短={args.short_period}, 长={args.long_period})")

    cerebro.adddata(data)

    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                        riskfreerate=0.03, annualize=True,
                        timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    # 运行回测
    print(f"\n初始资金: {args.cash:,.2f} 元")
    print(f"手续费率: {args.commission:.4%}")
    print("正在运行回测...\n")

    results = cerebro.run()

    # 输出绩效
    perf = print_performance(results, args.cash)

    # 保存报告
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("回测绩效报告\n")
        f.write(f"数据文件: {args.input}\n")
        f.write(f"策略: {args.strategy}\n")
        f.write(f"初始资金: {args.cash:,.2f}\n")
        f.write(f"期末净值: {perf['final_value']:,.2f}\n")
        f.write(f"总收益率: {perf['total_return']:.4%}\n")

    print(f"\n报告已保存至 {output_path}")


if __name__ == "__main__":
    main()
