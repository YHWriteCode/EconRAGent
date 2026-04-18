from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pandas as pd
import pytest


def _install_financial_skill_stubs(monkeypatch) -> None:
    akshare_module = types.ModuleType("akshare")
    akshare_module.stock_zh_a_hist = lambda **_kwargs: pd.DataFrame()
    linearmodels_module = types.ModuleType("linearmodels")
    linearmodels_panel_module = types.ModuleType("linearmodels.panel")
    linearmodels_panel_module.PanelOLS = object
    statsmodels_module = types.ModuleType("statsmodels")
    statsmodels_api_module = types.ModuleType("statsmodels.api")
    statsmodels_api_module.add_constant = lambda value: value
    backtrader_module = types.ModuleType("backtrader")
    backtrader_module.feeds = types.SimpleNamespace(PandasData=object)
    backtrader_module.Strategy = object
    backtrader_module.Cerebro = object
    backtrader_module.analyzers = types.SimpleNamespace(
        SharpeRatio=object,
        DrawDown=object,
        TradeAnalyzer=object,
        Returns=object,
    )

    monkeypatch.setitem(sys.modules, "akshare", akshare_module)
    monkeypatch.setitem(sys.modules, "linearmodels", linearmodels_module)
    monkeypatch.setitem(sys.modules, "linearmodels.panel", linearmodels_panel_module)
    monkeypatch.setitem(sys.modules, "statsmodels", statsmodels_module)
    monkeypatch.setitem(sys.modules, "statsmodels.api", statsmodels_api_module)
    monkeypatch.setitem(sys.modules, "backtrader", backtrader_module)


def _load_financial_skill_script(monkeypatch, relative_path: str) -> object:
    _install_financial_skill_stubs(monkeypatch)
    module_path = Path(relative_path).resolve()
    module_name = f"financial_researching_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load financial researching module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_financial_skill_module(monkeypatch) -> object:
    return _load_financial_skill_script(
        monkeypatch,
        "skills/financial-researching/scripts/fetch_model_backtest.py",
    )


def _akshare_frame(code: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2023-01-03", "2023-01-04", "2023-01-05", "2023-01-06", "2023-01-09"],
            "开盘": [10, 10.2, 10.4, 10.5, 10.6],
            "收盘": [10.1, 10.3, 10.2, 10.7, 10.8],
            "最高": [10.2, 10.4, 10.5, 10.8, 10.9],
            "最低": [9.9, 10.1, 10.0, 10.4, 10.5],
            "成交量": [1000, 1100, 1200, 1300, 1400],
            "成交额": [10000, 11300, 12240, 13910, 15120],
            "代码": [code] * 5,
        }
    )


def _yfinance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "High": [101.0, 102.0, 103.0, 104.0, 105.0],
            "Low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
            "Volume": [10000, 11000, 12000, 13000, 14000],
        },
        index=pd.to_datetime(
            ["2023-01-03", "2023-01-04", "2023-01-05", "2023-01-06", "2023-01-09"]
        ),
    )


def test_fetch_data_falls_back_to_yfinance_for_target_code(tmp_path: Path, monkeypatch):
    module = _load_financial_skill_module(monkeypatch)
    module.OUTPUT_DIR = tmp_path
    module.FETCH_RETRIES = 1
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    def _stock_hist(*, symbol, **_kwargs):
        if symbol == "300750":
            raise ConnectionError("akshare unavailable for target")
        return _akshare_frame(symbol)

    monkeypatch.setattr(module.ak, "stock_zh_a_hist", _stock_hist)

    yf_module = types.SimpleNamespace(
        download=lambda symbol, **_kwargs: _yfinance_frame() if symbol == "300750.SZ" else pd.DataFrame()
    )
    monkeypatch.setitem(sys.modules, "yfinance", yf_module)

    result = module.fetch_data(
        ["300750", "002594"],
        "20230103",
        "20230109",
        target_code="300750",
    )

    codes = set(result["code"].astype(str).str.zfill(6))
    assert "300750" in codes
    assert "002594" in codes
    target_rows = result[result["code"].astype(str).str.zfill(6) == "300750"]
    assert not target_rows.empty
    assert "amount" in target_rows.columns
    assert float(target_rows["amount"].iloc[0]) == 0.0


def test_fetch_data_exits_when_target_code_is_missing_after_fallback(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_financial_skill_module(monkeypatch)
    module.OUTPUT_DIR = tmp_path
    module.FETCH_RETRIES = 1
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    def _stock_hist(*, symbol, **_kwargs):
        if symbol == "300750":
            raise ConnectionError("akshare unavailable for target")
        return _akshare_frame(symbol)

    monkeypatch.setattr(module.ak, "stock_zh_a_hist", _stock_hist)

    yf_module = types.SimpleNamespace(download=lambda *_args, **_kwargs: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "yfinance", yf_module)

    with pytest.raises(SystemExit):
        module.fetch_data(
            ["300750", "002594"],
            "20230103",
            "20230109",
            target_code="300750",
        )


def test_analyze_stock_trend_reports_uptrend(monkeypatch):
    module = _load_financial_skill_script(
        monkeypatch,
        "skills/financial-researching/scripts/analyze_stock_trend.py",
    )

    rising = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-01-15",
                    "2026-02-15",
                    "2026-03-15",
                    "2026-04-15",
                ]
            ),
            "code": ["002594"] * 4,
            "open": [300.0, 315.0, 330.0, 345.0],
            "high": [305.0, 320.0, 335.0, 350.0],
            "low": [295.0, 310.0, 325.0, 340.0],
            "close": [302.0, 318.0, 336.0, 352.0],
            "volume": [1_000_000, 1_050_000, 1_080_000, 1_100_000],
            "amount": [3.02e8, 3.34e8, 3.63e8, 3.87e8],
        }
    )
    standardized = module.standardize_stock_frame(rising)
    result = module.analyze_stock(
        standardized,
        trend_start="20260115",
        trend_end="20260415",
    )
    summary = module.build_summary(result)

    assert result["trend"]["is_uptrend"] is True
    assert result["trend"]["label"] == "uptrend"
    assert "存在上涨趋势" in summary


def test_analyze_stock_trend_supports_us_ticker_via_yfinance(monkeypatch):
    module = _load_financial_skill_script(
        monkeypatch,
        "skills/financial-researching/scripts/analyze_stock_trend.py",
    )
    module.FETCH_RETRIES = 1
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    yf_module = types.SimpleNamespace(
        download=lambda symbol, **_kwargs: _yfinance_frame() if symbol == "TSLA" else pd.DataFrame()
    )
    monkeypatch.setitem(sys.modules, "yfinance", yf_module)

    result = module.fetch_stock_data("TSLA", "20230103", "20230109")

    assert module._yfinance_symbol_for_code("tsla") == "TSLA"
    assert set(result["code"].astype(str)) == {"TSLA"}


def test_fetch_market_data_falls_back_to_yfinance_for_a_share_network_failures(monkeypatch):
    module = _load_financial_skill_script(
        monkeypatch,
        "skills/financial-researching/scripts/fetch_market_data.py",
    )
    module.FETCH_RETRIES = 1
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    def _stock_hist(*, symbol, **_kwargs):
        if symbol in {"300750", "600519"}:
            raise ConnectionError("akshare unavailable")
        return _akshare_frame(symbol)

    monkeypatch.setattr(module.ak, "stock_zh_a_hist", _stock_hist)
    yf_module = types.SimpleNamespace(
        download=lambda symbol, **_kwargs: (
            _yfinance_frame()
            if symbol in {"300750.SZ", "600519.SS"}
            else pd.DataFrame()
        )
    )
    monkeypatch.setitem(sys.modules, "yfinance", yf_module)

    catl = module.fetch_single_stock("300750", "20230103", "20230109", "qfq")
    maotai = module.fetch_single_stock("600519", "20230103", "20230109", "qfq")
    result = module.standardize(pd.concat([catl, maotai], ignore_index=True))

    assert not result.empty
    assert set(result["code"].astype(str)) == {"300750", "600519"}
    assert float(result[result["code"] == "300750"]["amount"].iloc[0]) == 0.0
    assert float(result[result["code"] == "600519"]["amount"].iloc[0]) == 0.0


def test_fetch_market_data_supports_us_ticker_via_yfinance(monkeypatch):
    module = _load_financial_skill_script(
        monkeypatch,
        "skills/financial-researching/scripts/fetch_market_data.py",
    )
    module.FETCH_RETRIES = 1
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    yf_module = types.SimpleNamespace(
        download=lambda symbol, **_kwargs: _yfinance_frame() if symbol == "TSLA" else pd.DataFrame()
    )
    monkeypatch.setitem(sys.modules, "yfinance", yf_module)

    result = module.fetch_single_stock("TSLA", "20230103", "20230109", "qfq")
    standardized = module.standardize(result)

    assert module._yfinance_symbol_for_code("tsla") == "TSLA"
    assert set(standardized["code"].astype(str)) == {"TSLA"}
