"""
fetch_market_data.py
====================
职责：使用 AKShare 获取 A 股日线行情数据，执行标准化清洗后输出 CSV。

用法：
    python fetch_market_data.py --codes 000001,000002 --start 20220101 --end 20231231 --adjust qfq --output output/market_data.csv

依赖：akshare, pandas
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("错误：未安装 akshare，请执行 pip install akshare")
    sys.exit(1)


# ──────────────────────────────────────────────
# 列名映射（AKShare 原始列名 → 标准列名）
# 待按环境调整：AKShare 版本更新可能导致原始列名变化
# ──────────────────────────────────────────────
COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "price_change",
    "换手率": "turnover",
}


def fetch_single_stock(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """
    获取单只股票的日线行情数据。

    Parameters
    ----------
    code : str
        6 位股票代码，如 "000001"
    start : str
        开始日期，格式 YYYYMMDD
    end : str
        结束日期，格式 YYYYMMDD
    adjust : str
        复权类型："qfq"（前复权）、"hfq"（后复权）、""（不复权）

    Returns
    -------
    pd.DataFrame
        标准化后的行情数据
    """
    print(f"  正在获取 {code} 的日线数据 ({start} ~ {end}, 复权={adjust or '不复权'})...")

    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
    except Exception as e:
        print(f"  警告：获取 {code} 数据失败 - {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        print(f"  警告：{code} 返回空数据，请检查代码或日期范围")
        return pd.DataFrame()

    # 列名映射
    df = df.rename(columns=COLUMN_MAP)

    # 添加标准化代码列
    df["code"] = code

    return df


def standardize(df: pd.DataFrame) -> pd.DataFrame:
    """
    对行情数据执行标准化清洗。

    操作包括：
    1. 日期列转 datetime
    2. 确保代码列为字符串
    3. 成交量单位转换（如需要）
    4. 缺失值处理
    5. 计算日收益率
    6. 列筛选与排序
    """
    if df.empty:
        return df

    # 日期标准化
    df["date"] = pd.to_datetime(df["date"])

    # 代码标准化：确保 6 位字符串
    df["code"] = df["code"].astype(str).str.zfill(6)

    # 数值列转换
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 缺失值处理
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col].ffill()
    for col in ["volume", "amount"]:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    # 计算日收益率
    if "close" in df.columns:
        df = df.sort_values(["code", "date"])
        df["return"] = df.groupby("code")["close"].pct_change()
        df["return"] = df["return"].fillna(0.0)

    # 选择标准列并排序
    standard_cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "return"]
    available_cols = [c for c in standard_cols if c in df.columns]
    df = df[available_cols]
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    return df


def main():
    parser = argparse.ArgumentParser(description="使用 AKShare 获取 A 股日线行情数据")
    parser.add_argument("--codes", type=str, required=True,
                        help="股票代码列表，逗号分隔，如 000001,000002,600519")
    parser.add_argument("--start", type=str, required=True,
                        help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end", type=str, required=True,
                        help="结束日期，格式 YYYYMMDD")
    parser.add_argument("--adjust", type=str, default="qfq",
                        choices=["qfq", "hfq", ""],
                        help="复权类型：qfq(前复权), hfq(后复权), 空(不复权)，默认 qfq")
    parser.add_argument("--output", type=str, default="output/market_data.csv",
                        help="输出文件路径，默认 output/market_data.csv")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",")]
    print(f"目标股票：{codes}")
    print(f"日期范围：{args.start} ~ {args.end}")
    print(f"复权方式：{args.adjust or '不复权'}")
    print()

    # 逐只获取
    all_data = []
    for code in codes:
        df = fetch_single_stock(code, args.start, args.end, args.adjust)
        if not df.empty:
            all_data.append(df)

    if not all_data:
        print("错误：未获取到任何数据")
        sys.exit(1)

    # 合并与标准化
    combined = pd.concat(all_data, ignore_index=True)
    result = standardize(combined)

    # 输出
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n数据已保存至 {output_path}")
    print(f"数据形状：{result.shape}")
    print(f"股票数量：{result['code'].nunique()}")
    print(f"日期范围：{result['date'].min()} ~ {result['date'].max()}")


if __name__ == "__main__":
    main()
