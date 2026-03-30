from __future__ import annotations

from typing import Any

from kg_agent.tools.base import ToolResult


async def quant_backtest(
    *,
    query: str,
    symbol: str | None = None,
    strategy_name: str | None = None,
    **_: Any,
) -> ToolResult:
    return ToolResult(
        tool_name="quant_backtest",
        success=False,
        data={
            "status": "not_implemented",
            "query": query,
            "symbol": symbol,
            "strategy_name": strategy_name,
            "summary": "Quant backtest is reserved as a stage-one placeholder tool",
        },
        error="Quant backtest is not implemented yet",
        metadata={"implemented": False},
    )
