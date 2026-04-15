"""
analyze_stock_trend.py
======================
职责：抓取单只股票的日线行情，输出波动诊断与最近窗口趋势判断。

用法：
    python analyze_stock_trend.py --code 002594 --start 20250415 --end 20260415 --trend-start 20260115 --trend-end 20260415 --output output/byd_002594_trend.json

依赖：akshare, pandas, numpy
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("错误：未安装 akshare，请执行 pip install akshare")
    sys.exit(1)


FETCH_RETRIES = 3
FETCH_RETRY_BASE_DELAY_S = 1.0
YFINANCE_TIMEOUT_S = 20
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


def _normalize_code(code: str) -> str:
    return str(code).strip().zfill(6)


def _normalize_akshare_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    normalized = df.rename(columns=COLUMN_MAP).copy()
    normalized["code"] = _normalize_code(code)
    return normalized


def _yfinance_symbol_for_code(code: str) -> str:
    normalized = _normalize_code(code)
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
    return ak.stock_zh_a_hist(
        symbol=_normalize_code(code),
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


def fetch_stock_data(code: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    normalized_code = _normalize_code(code)
    last_error = None
    for source_name, fetcher in (
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
    ):
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                df = fetcher()
                if df is not None and not df.empty:
                    print(f"数据源 {source_name} 第 {attempt}/{FETCH_RETRIES} 次成功")
                    return standardize_stock_frame(df)
                last_error = RuntimeError("empty result")
            except Exception as exc:
                last_error = exc
                print(f"数据源 {source_name} 第 {attempt}/{FETCH_RETRIES} 次失败: {exc}")
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_BASE_DELAY_S * attempt)
    raise RuntimeError(f"无法获取 {normalized_code} 的行情数据: {last_error}")


def standardize_stock_frame(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date"]).copy()
    normalized["code"] = normalized["code"].astype(str).str.zfill(6)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    for column in ("open", "high", "low", "close"):
        if column in normalized.columns:
            normalized[column] = normalized[column].ffill()
    for column in ("volume", "amount"):
        if column in normalized.columns:
            normalized[column] = normalized[column].fillna(0)
    normalized = normalized.sort_values("date").reset_index(drop=True)
    normalized["return"] = normalized["close"].pct_change().fillna(0.0)
    normalized["rolling_vol_20"] = (
        normalized["return"].rolling(window=20, min_periods=5).std() * math.sqrt(252)
    )
    normalized["sma_20"] = normalized["close"].rolling(window=20, min_periods=5).mean()
    normalized["sma_60"] = normalized["close"].rolling(window=60, min_periods=10).mean()
    normalized["cummax_close"] = normalized["close"].cummax()
    normalized["drawdown"] = normalized["close"] / normalized["cummax_close"] - 1.0
    return normalized


def _safe_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _linear_slope(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 2:
        return None
    x = np.arange(len(clean), dtype=float)
    slope, _ = np.polyfit(x, clean.to_numpy(dtype=float), 1)
    return float(slope)


def analyze_stock(
    market_df: pd.DataFrame,
    *,
    trend_start: str | None = None,
    trend_end: str | None = None,
) -> dict[str, object]:
    if market_df.empty:
        raise ValueError("market_df is empty")

    overall_start = pd.to_datetime(market_df["date"].min()).date()
    overall_end = pd.to_datetime(market_df["date"].max()).date()
    trend_start_date = (
        pd.to_datetime(trend_start).date()
        if trend_start
        else max(overall_start, overall_end - timedelta(days=90))
    )
    trend_end_date = pd.to_datetime(trend_end).date() if trend_end else overall_end

    trend_df = market_df[
        (market_df["date"].dt.date >= trend_start_date)
        & (market_df["date"].dt.date <= trend_end_date)
    ].copy()
    if trend_df.empty:
        raise ValueError("trend window does not contain any market data")

    full_return = market_df["close"].iloc[-1] / market_df["close"].iloc[0] - 1.0
    trend_return = trend_df["close"].iloc[-1] / trend_df["close"].iloc[0] - 1.0
    annualized_volatility = market_df["return"].std(ddof=0) * math.sqrt(252)
    recent_volatility = trend_df["return"].std(ddof=0) * math.sqrt(252)
    max_drawdown = market_df["drawdown"].min()
    price_slope = _linear_slope(trend_df["close"])
    recent_sma_20 = _safe_float(trend_df["sma_20"].iloc[-1])
    recent_sma_60 = _safe_float(trend_df["sma_60"].iloc[-1])
    last_close = _safe_float(trend_df["close"].iloc[-1])

    sma_signal = None
    if recent_sma_20 is not None and recent_sma_60 is not None:
        sma_signal = recent_sma_20 >= recent_sma_60
    is_uptrend = bool(
        trend_return > 0
        and (price_slope or 0.0) > 0
        and (sma_signal is None or sma_signal)
    )
    trend_label = "uptrend" if is_uptrend else "not_uptrend"

    return {
        "code": str(market_df["code"].iloc[0]).zfill(6),
        "overall_window": {
            "start": overall_start.isoformat(),
            "end": overall_end.isoformat(),
        },
        "trend_window": {
            "start": trend_start_date.isoformat(),
            "end": trend_end_date.isoformat(),
        },
        "volatility": {
            "annualized": float(annualized_volatility),
            "trend_window_annualized": float(recent_volatility),
            "max_drawdown": float(max_drawdown),
        },
        "trend": {
            "label": trend_label,
            "is_uptrend": is_uptrend,
            "trend_window_return": float(trend_return),
            "full_window_return": float(full_return),
            "price_slope": price_slope,
            "last_close": last_close,
            "sma20": recent_sma_20,
            "sma60": recent_sma_60,
        },
    }


def build_summary(result: dict[str, object]) -> str:
    code = str(result["code"])
    overall_window = result["overall_window"]
    trend_window = result["trend_window"]
    volatility = result["volatility"]
    trend = result["trend"]
    label = "存在上涨趋势" if trend["is_uptrend"] else "未形成明确上涨趋势"
    return (
        f"标的 {code} 在 {overall_window['start']} 至 {overall_window['end']} 的区间内，"
        f"年化波动率约为 {volatility['annualized']:.2%}，最大回撤约为 {abs(volatility['max_drawdown']):.2%}。"
        f"在 {trend_window['start']} 至 {trend_window['end']} 的趋势窗口内，"
        f"区间收益率约为 {trend['trend_window_return']:.2%}，判断结果为：{label}。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="分析单只股票的波动率和趋势")
    parser.add_argument("--code", type=str, required=True, help="6 位股票代码，如 002594")
    parser.add_argument("--start", type=str, required=True, help="开始日期，格式 YYYYMMDD")
    parser.add_argument("--end", type=str, required=True, help="结束日期，格式 YYYYMMDD")
    parser.add_argument(
        "--trend-start",
        type=str,
        default="",
        help="趋势判断窗口开始日期，格式 YYYYMMDD；留空则默认使用末尾约 3 个月",
    )
    parser.add_argument(
        "--trend-end",
        type=str,
        default="",
        help="趋势判断窗口结束日期，格式 YYYYMMDD；留空则默认使用主窗口结束日期",
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="qfq",
        choices=["qfq", "hfq", ""],
        help="复权类型：qfq(前复权), hfq(后复权), 空(不复权)，默认 qfq",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output/stock_trend_analysis.json",
        help="分析结果输出路径，默认 output/stock_trend_analysis.json",
    )
    args = parser.parse_args()

    market_df = fetch_stock_data(
        code=args.code,
        start=args.start,
        end=args.end,
        adjust=args.adjust,
    )
    result = analyze_stock(
        market_df,
        trend_start=args.trend_start or None,
        trend_end=args.trend_end or None,
    )
    summary = build_summary(result)
    result["summary"] = summary

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(summary)
    print(f"分析结果已保存至 {output_path}")


if __name__ == "__main__":
    main()
