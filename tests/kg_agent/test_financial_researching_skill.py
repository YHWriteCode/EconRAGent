from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pandas as pd
import pytest


def _load_financial_skill_module(monkeypatch) -> object:
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

    module_path = Path("skills/financial-researching/scripts/fetch_model_backtest.py").resolve()
    module_name = f"financial_researching_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load financial researching module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
