"""
fetch_model_backtest.py
=======================
职责：端到端脚本，串联"取数 → 清洗 → 面板构建 → 回归建模 → 信号生成 → 回测"全链路。

用法：
    python fetch_model_backtest.py --codes 000001,000002,600519 --start 20210101 --end 20231231

依赖：akshare, pandas, linearmodels, statsmodels, backtrader

注意：这是 MVP 版本，演示完整流程。各步骤函数设计为可独立调用、可替换。
"""

import os
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("错误：未安装 akshare，请执行 pip install akshare")
    sys.exit(1)

try:
    from linearmodels.panel import PanelOLS
except ImportError:
    print("错误：未安装 linearmodels，请执行 pip install linearmodels")
    sys.exit(1)

try:
    import statsmodels.api as sm
except ImportError:
    print("错误：未安装 statsmodels，请执行 pip install statsmodels")
    sys.exit(1)

try:
    import backtrader as bt
except ImportError:
    print("错误：未安装 backtrader，请执行 pip install backtrader")
    sys.exit(1)

import numpy as np

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────
OUTPUT_DIR = Path("output")
INITIAL_CASH = 1_000_000
COMMISSION = 0.001  # 0.1%

# AKShare 列名映射（待按环境调整）
COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_change",
    "换手率": "turnover",
}


# ══════════════════════════════════════════════
# 第一步：数据获取与清洗
# ══════════════════════════════════════════════
def fetch_data(codes: list, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    """
    批量获取股票日线数据并执行标准化清洗。

    Returns
    -------
    pd.DataFrame
        标准化后的行情数据（包含 date, code, open, high, low, close, volume, amount, return）
    """
    print("=" * 55)
    print("第一步：数据获取与清洗")
    print("=" * 55)

    all_data = []
    for code in codes:
        print(f"  获取 {code}...")
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start, end_date=end,
                adjust=adjust,
            )
            if df is not None and not df.empty:
                df = df.rename(columns=COLUMN_MAP)
                df["code"] = code
                all_data.append(df)
                print(f"    ✓ {len(df)} 行")
            else:
                print(f"    ✗ 返回空数据")
        except Exception as e:
            print(f"    ✗ 获取失败: {e}")

    if not all_data:
        print("错误：无法获取任何数据")
        sys.exit(1)

    combined = pd.concat(all_data, ignore_index=True)

    # 标准化
    combined["date"] = pd.to_datetime(combined["date"])
    combined["code"] = combined["code"].astype(str).str.zfill(6)

    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")

    # 缺失值处理
    for col in ["open", "high", "low", "close"]:
        if col in combined.columns:
            combined[col] = combined[col].ffill()
    for col in ["volume", "amount"]:
        if col in combined.columns:
            combined[col] = combined[col].fillna(0)

    # 日收益率
    combined = combined.sort_values(["code", "date"])
    combined["return"] = combined.groupby("code")["close"].pct_change().fillna(0.0)

    # 保留标准列
    std_cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "return"]
    available = [c for c in std_cols if c in combined.columns]
    combined = combined[available].reset_index(drop=True)

    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUTPUT_DIR / "market_data.csv", index=False, encoding="utf-8-sig")
    print(f"\n  数据已保存: {OUTPUT_DIR / 'market_data.csv'}")
    print(f"  总行数: {len(combined)}, 股票数: {combined['code'].nunique()}")
    return combined


# ══════════════════════════════════════════════
# 第二步：构建面板数据
# ══════════════════════════════════════════════
def prepare_panel(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算因子并构建面板格式数据。

    计算的因子：
    - momentum: 过去 20 日累计收益
    - volatility: 过去 20 日收益率标准差
    - size_factor: 成交额对数（规模代理）

    Returns
    -------
    pd.DataFrame
        MultiIndex (code, date) 的面板数据
    """
    print("\n" + "=" * 55)
    print("第二步：构建面板数据")
    print("=" * 55)

    df = df.sort_values(["code", "date"]).copy()

    # 动量因子
    df["momentum"] = df.groupby("code")["return"].transform(
        lambda x: x.rolling(window=20, min_periods=5).sum()
    )

    # 波动率因子
    df["volatility"] = df.groupby("code")["return"].transform(
        lambda x: x.rolling(window=20, min_periods=5).std()
    )

    # 规模因子
    if "amount" in df.columns:
        df["size_factor"] = df.groupby("code")["amount"].transform(
            lambda x: x.rolling(window=20, min_periods=5).mean()
        ).apply(lambda x: np.log(x) if x > 0 else np.nan)
    else:
        df["size_factor"] = np.log(df["close"].clip(lower=0.01))

    # 构建面板
    panel_cols = ["date", "code", "return", "close", "momentum", "volatility", "size_factor"]
    available = [c for c in panel_cols if c in df.columns]
    panel = df[available].dropna().drop_duplicates(subset=["code", "date"])
    panel = panel.set_index(["code", "date"]).sort_index()

    panel.to_csv(OUTPUT_DIR / "panel_data.csv", encoding="utf-8-sig")
    print(f"  面板数据已保存: {OUTPUT_DIR / 'panel_data.csv'}")
    print(f"  面板形状: {panel.shape}")
    print(f"  个体数: {panel.index.get_level_values(0).nunique()}")
    return panel


# ══════════════════════════════════════════════
# 第三步：面板回归建模
# ══════════════════════════════════════════════
def run_model(panel: pd.DataFrame,
              dep: str = "return",
              indep: list = None) -> dict:
    """
    执行固定效应面板回归，返回显著因子及其系数方向。

    Returns
    -------
    dict
        {
            "result": PanelOLS result object,
            "significant_factors": [(name, coef, pvalue), ...],
        }
    """
    print("\n" + "=" * 55)
    print("第三步：面板回归建模")
    print("=" * 55)

    if indep is None:
        indep = ["momentum", "volatility", "size_factor"]

    # 检查变量
    all_vars = [dep] + indep
    missing = [v for v in all_vars if v not in panel.columns]
    if missing:
        print(f"错误：变量不存在 - {missing}")
        sys.exit(1)

    subset = panel[all_vars].dropna()
    print(f"  因变量: {dep}")
    print(f"  自变量: {indep}")
    print(f"  有效观测: {len(subset)}")

    if len(subset) < 50:
        print("警告：观测数不足，回归结果可能不可靠")

    y = subset[dep]
    X = sm.add_constant(subset[indep])

    model = PanelOLS(dependent=y, exog=X, entity_effects=True)
    result = model.fit(cov_type="clustered", cluster_entity=True)

    print("\n" + str(result.summary))

    # 提取显著因子
    significant = []
    for var in indep:
        if var in result.params.index:
            coef = result.params[var]
            pval = result.pvalues[var]
            if pval < 0.05:
                significant.append((var, coef, pval))
                print(f"  ✓ 显著因子: {var} (coef={coef:.6f}, p={pval:.4f})")

    if not significant:
        print("  注意：无因子在 5% 水平显著")

    # 保存结果
    with open(OUTPUT_DIR / "regression_summary.txt", "w", encoding="utf-8") as f:
        f.write(str(result.summary))

    return {"result": result, "significant_factors": significant}


# ══════════════════════════════════════════════
# 第四步：生成交易信号
# ══════════════════════════════════════════════
def generate_signal(market_df: pd.DataFrame,
                    model_result: dict,
                    target_code: str) -> pd.DataFrame:
    """
    基于回归结果生成交易信号。

    策略逻辑（MVP）：
    - 取第一个显著因子
    - 因子值 > 中位数 → 买入信号 (1)
    - 因子值 < 中位数 → 卖出信号 (-1)
    - 其余 → 持有 (0)

    如果没有显著因子，回退到简单动量策略。

    Returns
    -------
    pd.DataFrame
        包含 date, signal 列的信号数据（针对 target_code）
    """
    print("\n" + "=" * 55)
    print("第四步：生成交易信号")
    print("=" * 55)

    # 筛选目标股票
    stock_df = market_df[market_df["code"] == target_code].copy()
    if stock_df.empty:
        print(f"错误：未找到标的 {target_code} 的数据")
        sys.exit(1)

    stock_df = stock_df.sort_values("date").reset_index(drop=True)

    significant = model_result.get("significant_factors", [])

    if significant:
        # 使用第一个显著因子
        factor_name, coef, _ = significant[0]
        print(f"  使用因子: {factor_name} (coef={coef:.6f})")

        # 需要重新计算因子（因为 market_df 是原始行情数据）
        if factor_name == "momentum":
            stock_df["factor"] = stock_df["return"].rolling(20, min_periods=5).sum()
        elif factor_name == "volatility":
            stock_df["factor"] = stock_df["return"].rolling(20, min_periods=5).std()
        elif factor_name == "size_factor":
            if "amount" in stock_df.columns:
                stock_df["factor"] = stock_df["amount"].rolling(20, min_periods=5).mean().apply(
                    lambda x: np.log(x) if x > 0 else np.nan
                )
            else:
                stock_df["factor"] = np.log(stock_df["close"].clip(lower=0.01))
        else:
            print(f"  未识别的因子 {factor_name}，回退到动量")
            stock_df["factor"] = stock_df["return"].rolling(20, min_periods=5).sum()

        # 生成信号：因子方向取决于系数符号
        median_val = stock_df["factor"].median()
        if coef > 0:
            stock_df["signal"] = stock_df["factor"].apply(
                lambda x: 1 if x > median_val else (-1 if x < median_val else 0)
            )
        else:
            stock_df["signal"] = stock_df["factor"].apply(
                lambda x: -1 if x > median_val else (1 if x < median_val else 0)
            )
    else:
        # 回退策略：简单 20 日动量
        print("  无显著因子，回退到 20 日动量策略")
        stock_df["factor"] = stock_df["return"].rolling(20, min_periods=5).sum()
        stock_df["signal"] = stock_df["factor"].apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )

    # 填充 NaN
    stock_df["signal"] = stock_df["signal"].fillna(0).astype(int)

    signals = stock_df[["date", "signal"]].copy()
    buy_count = (signals["signal"] == 1).sum()
    sell_count = (signals["signal"] == -1).sum()
    print(f"  信号生成完毕: 买入={buy_count}, 卖出={sell_count}")

    signals.to_csv(OUTPUT_DIR / "signals.csv", index=False, encoding="utf-8-sig")
    print(f"  信号已保存: {OUTPUT_DIR / 'signals.csv'}")

    return signals


# ══════════════════════════════════════════════
# 第五步：执行回测
# ══════════════════════════════════════════════
class PipelineSignalData(bt.feeds.PandasData):
    """自定义 Data Feed，包含 signal 行"""
    lines = ("signal",)
    params = (("signal", -1),)


class PipelineStrategy(bt.Strategy):
    """基于外部信号的回测策略"""

    def __init__(self):
        self.signal = self.data.signal
        self.order = None

    def notify_order(self, order):
        if order.status in [order.Completed]:
            pass  # 静默执行，避免大量输出
        self.order = None

    def next(self):
        if self.order:
            return
        if not self.position and self.signal[0] == 1:
            self.order = self.buy()
        elif self.position and self.signal[0] == -1:
            self.order = self.sell()


def run_backtest(market_df: pd.DataFrame, signals: pd.DataFrame,
                 target_code: str) -> dict:
    """
    执行回测并输出绩效报告。

    Returns
    -------
    dict
        绩效摘要
    """
    print("\n" + "=" * 55)
    print("第五步：执行回测")
    print("=" * 55)

    # 准备数据
    stock_df = market_df[market_df["code"] == target_code].copy()
    stock_df = stock_df.sort_values("date").reset_index(drop=True)

    # 合并信号
    stock_df = stock_df.merge(signals[["date", "signal"]], on="date", how="left")
    stock_df["signal"] = stock_df["signal"].fillna(0).astype(int)

    # 确保必要列
    bt_cols = ["date", "open", "high", "low", "close", "volume", "signal"]
    for c in bt_cols:
        if c not in stock_df.columns:
            print(f"错误：缺少列 {c}")
            sys.exit(1)

    stock_df["openinterest"] = 0
    stock_df = stock_df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    # 构建 Cerebro
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.setcommission(commission=COMMISSION)

    data = PipelineSignalData(dataname=stock_df, datetime="date")
    cerebro.adddata(data)
    cerebro.addstrategy(PipelineStrategy)

    # 分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                        riskfreerate=0.03, annualize=True,
                        timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

    print(f"  初始资金: {INITIAL_CASH:,.2f} 元")
    print(f"  手续费率: {COMMISSION:.4%}")
    print(f"  回测标的: {target_code}")
    print(f"  数据行数: {len(stock_df)}")
    print("  正在回测...\n")

    results = cerebro.run()
    strat = results[0]

    final_value = strat.broker.getvalue()
    total_return = (final_value - INITIAL_CASH) / INITIAL_CASH

    # 收集绩效
    perf = {
        "initial_cash": INITIAL_CASH,
        "final_value": final_value,
        "total_return": total_return,
    }

    try:
        ret = strat.analyzers.returns.get_analysis()
        perf["annual_return"] = ret.get("rnorm", None)
    except Exception:
        perf["annual_return"] = None

    try:
        sharpe = strat.analyzers.sharpe.get_analysis()
        perf["sharpe_ratio"] = sharpe.get("sharperatio", None)
    except Exception:
        perf["sharpe_ratio"] = None

    try:
        dd = strat.analyzers.drawdown.get_analysis()
        perf["max_drawdown"] = dd.get("max", {}).get("drawdown", None)
    except Exception:
        perf["max_drawdown"] = None

    try:
        ta = strat.analyzers.trades.get_analysis()
        total_trades = ta.get("total", {}).get("total", 0)
        won = ta.get("won", {}).get("total", 0)
        perf["total_trades"] = total_trades
        perf["win_rate"] = won / total_trades if total_trades > 0 else 0
    except Exception:
        perf["total_trades"] = 0
        perf["win_rate"] = 0

    return perf


# ══════════════════════════════════════════════
# 第六步：输出报告
# ══════════════════════════════════════════════
def report(perf: dict, model_result: dict, target_code: str):
    """输出最终绩效摘要"""
    print("\n" + "=" * 55)
    print("          端 到 端 研 究 报 告")
    print("=" * 55)
    print(f"  回测标的:       {target_code}")
    print(f"  初始资金:       {perf['initial_cash']:>15,.2f} 元")
    print(f"  期末净值:       {perf['final_value']:>15,.2f} 元")
    print(f"  总收益率:       {perf['total_return']:>14.4%}")

    if perf.get("annual_return") is not None:
        print(f"  年化收益率:     {perf['annual_return']:>14.4%}")
    if perf.get("sharpe_ratio") is not None:
        print(f"  Sharpe 比率:    {perf['sharpe_ratio']:>14.4f}")
    if perf.get("max_drawdown") is not None:
        print(f"  最大回撤:       {perf['max_drawdown']:>13.2f}%")

    print(f"  交易次数:       {perf.get('total_trades', 0):>12d}")
    print(f"  胜率:           {perf.get('win_rate', 0):>14.2%}")

    sig_factors = model_result.get("significant_factors", [])
    if sig_factors:
        print(f"\n  显著因子:")
        for name, coef, pval in sig_factors:
            direction = "正向" if coef > 0 else "负向"
            print(f"    - {name}: {direction} (coef={coef:.6f}, p={pval:.4f})")
    else:
        print(f"\n  显著因子: 无（使用动量回退策略）")

    print("=" * 55)

    # 保存报告到文件
    report_path = OUTPUT_DIR / "e2e_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("端到端研究报告\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"回测标的: {target_code}\n")
        f.write(f"总收益率: {perf['total_return']:.4%}\n")
        f.write(f"Sharpe: {perf.get('sharpe_ratio', 'N/A')}\n")
        f.write(f"最大回撤: {perf.get('max_drawdown', 'N/A')}\n")
        f.write(f"交易次数: {perf.get('total_trades', 0)}\n")
        f.write(f"胜率: {perf.get('win_rate', 0):.2%}\n")

    print(f"\n  报告已保存: {report_path}")


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════
def main():
    import argparse

    parser = argparse.ArgumentParser(description="端到端金融研究流程：取数→建模→回测")
    parser.add_argument("--codes", type=str, default="000001,000002,600519",
                        help="股票代码列表(逗号分隔)，默认 000001,000002,600519")
    parser.add_argument("--start", type=str, default="20210101",
                        help="开始日期 YYYYMMDD，默认 20210101")
    parser.add_argument("--end", type=str, default="20231231",
                        help="结束日期 YYYYMMDD，默认 20231231")
    parser.add_argument("--target", type=str, default=None,
                        help="回测目标代码，默认取第一个")
    parser.add_argument("--dep", type=str, default="return",
                        help="回归因变量，默认 return")
    parser.add_argument("--indep", type=str, default="momentum,volatility,size_factor",
                        help="回归自变量(逗号分隔)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",")]
    indep = [v.strip() for v in args.indep.split(",")]
    target_code = args.target or codes[0]

    print(f"\n{'#' * 55}")
    print(f"#  Financial Researching — 端到端流程")
    print(f"#  股票: {codes}")
    print(f"#  日期: {args.start} ~ {args.end}")
    print(f"#  回测标的: {target_code}")
    print(f"{'#' * 55}\n")

    # 执行流水线
    market_df = fetch_data(codes, args.start, args.end)
    panel = prepare_panel(market_df)
    model_result = run_model(panel, dep=args.dep, indep=indep)
    signals = generate_signal(market_df, model_result, target_code)
    perf = run_backtest(market_df, signals, target_code)
    report(perf, model_result, target_code)

    print("\n流程完成。所有输出文件在 output/ 目录下。")


if __name__ == "__main__":
    main()
