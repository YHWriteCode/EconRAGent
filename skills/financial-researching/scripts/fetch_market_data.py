"""
fetch_market_data.py
====================
职责：抓取股票日线行情数据，优先使用 AKShare 获取 A 股，并在失败时回退到 yfinance。

用法：
    python fetch_market_data.py --codes 000001,000002 --start 20220101 --end 20231231 --adjust qfq --output output/market_data.csv

依赖：akshare, pandas
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("错误：未安装 akshare，请执行 pip install akshare")
    sys.exit(1)


FETCH_RETRIES = 3
FETCH_RETRY_BASE_DELAY_S = 1.0
YFINANCE_TIMEOUT_S = 20
ALPHABETIC_TICKER_SUPPORTED = True

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


def _normalize_code(code: str) -> str:
    normalized = str(code).strip()
    if not normalized:
        raise ValueError("股票代码不能为空")
    if re.fullmatch(r"\d{1,6}", normalized):
        return normalized.zfill(6)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,9}", normalized):
        return normalized.upper()
    raise ValueError(f"不支持的股票代码格式: {normalized}")


def _is_numeric_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", _normalize_code(code)))


def _normalize_akshare_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    normalized = df.rename(columns=COLUMN_MAP).copy()
    normalized["code"] = _normalize_code(code)
    return normalized


def _yfinance_symbol_for_code(code: str) -> str:
    normalized = _normalize_code(code)
    if not _is_numeric_code(normalized):
        return normalized
    if normalized.startswith(("4", "8")):
        return f"{normalized}.BJ"
    if normalized.startswith(("5", "6", "9")):
        return f"{normalized}.SS"
    return f"{normalized}.SZ"


def _normalize_yfinance_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    normalized = df.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = normalized.columns.get_level_values(0)
    normalized = normalized.reset_index()
    date_col = next(
        (
            column
            for column in normalized.columns
            if str(column).strip().lower() in {"date", "datetime"}
        ),
        None,
    )
    if date_col is None and len(normalized.columns) > 0:
        first_column = normalized.columns[0]
        first_series = normalized[first_column]
        if str(first_column).strip().lower() == "index" or pd.api.types.is_datetime64_any_dtype(
            first_series
        ):
            date_col = first_column
    if date_col is None:
        raise ValueError("yfinance result does not contain a date column")

    normalized = normalized.rename(
        columns={
            date_col: "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    missing = [
        column
        for column in ("date", "open", "high", "low", "close", "volume")
        if column not in normalized.columns
    ]
    if missing:
        raise ValueError(f"yfinance result is missing required columns: {missing}")

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if getattr(normalized["date"].dt, "tz", None) is not None:
        normalized["date"] = normalized["date"].dt.tz_localize(None)
    normalized = normalized.dropna(subset=["date"]).copy()
    normalized["amount"] = 0.0
    normalized["code"] = _normalize_code(code)
    return normalized


def _fetch_from_akshare(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    normalized_code = _normalize_code(code)
    if not _is_numeric_code(normalized_code):
        raise ValueError(f"akshare 仅支持 A 股 6 位代码，当前标的为 {normalized_code}")
    return ak.stock_zh_a_hist(
        symbol=normalized_code,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust,
    )


def _fetch_from_yfinance(code: str, start: str, end: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed") from exc

    start_dt = datetime.strptime(start, "%Y%m%d")
    end_dt = datetime.strptime(end, "%Y%m%d") + timedelta(days=1)
    return yf.download(
        _yfinance_symbol_for_code(code),
        start=start_dt.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
        timeout=YFINANCE_TIMEOUT_S,
        threads=False,
    )


def fetch_single_stock(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    normalized_code = _normalize_code(code)
    print(f"  正在获取 {normalized_code} 的日线数据 ({start} ~ {end}, 复权={adjust or '不复权'})...")
    last_error = None

    sources = (
        (
            "akshare",
            lambda: _normalize_akshare_frame(
                _fetch_from_akshare(normalized_code, start, end, adjust),
                normalized_code,
            ),
        ),
        (
            "yfinance",
            lambda: _normalize_yfinance_frame(
                _fetch_from_yfinance(normalized_code, start, end),
                normalized_code,
            ),
        ),
    )

    for source_name, fetcher in sources:
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                df = fetcher()
                if df is not None and not df.empty:
                    print(f"  数据源 {source_name} 第 {attempt}/{FETCH_RETRIES} 次成功")
                    return df
                last_error = RuntimeError("empty result")
                print(f"  数据源 {source_name} 第 {attempt}/{FETCH_RETRIES} 次返回空数据")
            except Exception as exc:
                last_error = exc
                print(f"  数据源 {source_name} 第 {attempt}/{FETCH_RETRIES} 次失败: {exc}")
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_BASE_DELAY_S * attempt)

    print(f"  警告：获取 {normalized_code} 数据失败 - {last_error}")
    return pd.DataFrame()


def standardize(df: pd.DataFrame) -> pd.DataFrame:
    """
    对行情数据执行标准化清洗。

    操作包括：
    1. 日期列转 datetime
    2. 确保代码列标准化
    3. 缺失值处理
    4. 计算日收益率
    5. 列筛选与排序
    """
    if df.empty:
        return df

    normalized = df.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date"]).copy()
    normalized["code"] = normalized["code"].astype(str).map(_normalize_code)

    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")

    for col in ["open", "high", "low", "close"]:
        if col in normalized.columns:
            normalized[col] = normalized[col].ffill()
    for col in ["volume", "amount"]:
        if col in normalized.columns:
            normalized[col] = normalized[col].fillna(0)

    if "close" in normalized.columns:
        normalized = normalized.sort_values(["code", "date"])
        normalized["return"] = normalized.groupby("code")["close"].pct_change().fillna(0.0)

    standard_cols = ["date", "code", "open", "high", "low", "close", "volume", "amount", "return"]
    available_cols = [c for c in standard_cols if c in normalized.columns]
    normalized = normalized[available_cols]
    normalized = normalized.sort_values(["code", "date"]).reset_index(drop=True)
    return normalized


def main():
    parser = argparse.ArgumentParser(description="抓取股票日线行情数据")
    parser.add_argument("--codes", type=str, required=True,
                        help="股票代码列表，逗号分隔，如 000001,000002,600519 或 TSLA")
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

    codes = [_normalize_code(code) for code in args.codes.split(",") if code.strip()]
    print(f"目标股票：{codes}")
    print(f"日期范围：{args.start} ~ {args.end}")
    print(f"复权方式：{args.adjust or '不复权'}")
    print()

    all_data = []
    for code in codes:
        df = fetch_single_stock(code, args.start, args.end, args.adjust)
        if not df.empty:
            all_data.append(df)

    if not all_data:
        print("错误：未获取到任何数据")
        sys.exit(1)

    combined = pd.concat(all_data, ignore_index=True)
    result = standardize(combined)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n数据已保存至 {output_path}")
    print(f"数据形状：{result.shape}")
    print(f"股票数量：{result['code'].nunique()}")
    print(f"日期范围：{result['date'].min()} ~ {result['date'].max()}")


if __name__ == "__main__":
    main()
