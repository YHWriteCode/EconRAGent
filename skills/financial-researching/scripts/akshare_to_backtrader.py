"""
akshare_to_backtrader.py
========================
职责：将 AKShare 标准化行情数据转换为 backtrader 可直接消费的格式。

用法：
    python akshare_to_backtrader.py --input output/market_data.csv --code 000001 --output output/bt_000001.csv
    python akshare_to_backtrader.py --input output/market_data.csv --code 000001 --signal output/signals.csv --output output/bt_000001.csv

依赖：pandas
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


def load_market_data(filepath: str) -> pd.DataFrame:
    """加载标准化行情数据"""
    if not os.path.exists(filepath):
        print(f"错误：文件不存在 - {filepath}")
        sys.exit(1)
    df = pd.read_csv(filepath, parse_dates=["date"])
    return df


def filter_single_stock(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """
    从多股票数据中筛选单只股票。
    backtrader 每次只接受单只股票的数据馈送。
    """
    df["code"] = df["code"].astype(str).str.zfill(6)
    single = df[df["code"] == code].copy()

    if single.empty:
        available = df["code"].unique().tolist()
        print(f"错误：未找到代码 {code}，可用代码：{available}")
        sys.exit(1)

    print(f"已筛选 {code}，共 {len(single)} 条记录")
    return single


def convert_to_bt_format(df: pd.DataFrame) -> pd.DataFrame:
    """
    转换为 backtrader 标准格式。

    backtrader PandasData 要求：
    - datetime 列（或索引）
    - open, high, low, close, volume 列
    - 可选：openinterest 列
    - 按日期升序排列
    - 不允许 NaN
    """
    required_cols = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(f"错误：缺少 backtrader 必要列 - {missing}")
        sys.exit(1)

    bt_df = df[required_cols].copy()

    # 确保日期格式
    bt_df["date"] = pd.to_datetime(bt_df["date"])

    # 按日期升序
    bt_df = bt_df.sort_values("date").reset_index(drop=True)

    # 添加 openinterest 列（backtrader 需要，股票设为 0）
    bt_df["openinterest"] = 0

    # 清除 NaN（backtrader 不接受）
    nan_count = bt_df.isna().sum().sum()
    if nan_count > 0:
        print(f"警告：发现 {nan_count} 个 NaN 值，正在用前值填充...")
        bt_df = bt_df.ffill().bfill()

    # 最终验证
    assert bt_df.isna().sum().sum() == 0, "数据中仍有 NaN，无法用于 backtrader"

    return bt_df


def merge_signals(bt_df: pd.DataFrame, signal_path: str) -> pd.DataFrame:
    """
    将外部交易信号合并到行情数据中。

    信号文件格式要求：
    - CSV 文件
    - 包含 date 和 signal 列
    - signal: 1=买入, -1=卖出, 0=持有
    """
    if not os.path.exists(signal_path):
        print(f"警告：信号文件不存在 - {signal_path}，跳过信号合并")
        return bt_df

    signals = pd.read_csv(signal_path, parse_dates=["date"])

    if "signal" not in signals.columns:
        print("警告：信号文件中缺少 signal 列，跳过合并")
        return bt_df

    # 按日期合并
    bt_df = bt_df.merge(signals[["date", "signal"]], on="date", how="left")
    bt_df["signal"] = bt_df["signal"].fillna(0).astype(int)

    print(f"已合并信号：买入={len(bt_df[bt_df['signal'] == 1])}, "
          f"卖出={len(bt_df[bt_df['signal'] == -1])}, "
          f"持有={len(bt_df[bt_df['signal'] == 0])}")

    return bt_df


def main():
    parser = argparse.ArgumentParser(description="将 AKShare 数据转为 backtrader 格式")
    parser.add_argument("--input", type=str, required=True,
                        help="标准化行情数据 CSV 路径")
    parser.add_argument("--code", type=str, required=True,
                        help="目标股票代码（6 位），如 000001")
    parser.add_argument("--signal", type=str, default=None,
                        help="可选：交易信号 CSV 路径")
    parser.add_argument("--output", type=str, default=None,
                        help="输出文件路径，默认 output/bt_{code}.csv")
    args = parser.parse_args()

    if args.output is None:
        args.output = f"output/bt_{args.code}.csv"

    # 加载并筛选
    df = load_market_data(args.input)
    single = filter_single_stock(df, args.code)

    # 格式转换
    bt_df = convert_to_bt_format(single)

    # 可选信号合并
    if args.signal:
        bt_df = merge_signals(bt_df, args.signal)

    # 输出
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bt_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"\nbacktrader 数据已保存至 {output_path}")
    print(f"数据行数：{len(bt_df)}")
    print(f"日期范围：{bt_df['date'].min()} ~ {bt_df['date'].max()}")
    print(f"列：{list(bt_df.columns)}")


if __name__ == "__main__":
    main()
