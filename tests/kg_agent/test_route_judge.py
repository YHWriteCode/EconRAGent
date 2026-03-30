import pytest

from kg_agent.agent.route_judge import RouteJudge


@pytest.mark.asyncio
async def test_route_judge_relation_explanation_prefers_hybrid_and_graph_trace():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        session_context={"history": []},
        user_profile={},
        available_tools=["kg_hybrid_search", "graph_relation_trace"],
    )

    assert route.need_tools is True
    assert route.need_path_explanation is True
    assert [item.tool for item in route.tool_sequence] == [
        "kg_hybrid_search",
        "graph_relation_trace",
    ]


@pytest.mark.asyncio
async def test_route_judge_followup_prefers_memory_first():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="继续刚才关于比亚迪的话题",
        session_context={"history": [{"role": "user", "content": "比亚迪是什么公司"}]},
        user_profile={},
        available_tools=["memory_search", "kg_hybrid_search"],
    )

    assert route.need_memory is True
    assert route.tool_sequence[0].tool == "memory_search"


@pytest.mark.asyncio
async def test_route_judge_quant_request_uses_quant_tool():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="帮我回测一个双均线策略，并给出夏普比率",
        session_context={"history": []},
        user_profile={},
        available_tools=["quant_backtest"],
    )

    assert route.strategy == "quant_request"
    assert route.tool_sequence[0].tool == "quant_backtest"
