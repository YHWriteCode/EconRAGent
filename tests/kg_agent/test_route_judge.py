import pytest

from kg_agent.agent.route_judge import RouteJudge
from kg_agent.agent.prompts import (
    DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    build_route_judge_prompt,
    list_route_judge_prompt_versions,
)


@pytest.mark.asyncio
async def test_route_judge_relation_explanation_prefers_hybrid_and_graph_trace():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search", "graph_relation_trace"],
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
        available_capabilities=["memory_search", "kg_hybrid_search"],
    )

    assert route.need_memory is True
    assert route.tool_sequence[0].tool == "memory_search"


@pytest.mark.asyncio
async def test_route_judge_followup_can_use_cross_session_memory():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="continue the previous supplier topic",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["cross_session_search", "kg_hybrid_search"],
    )

    assert route.need_memory is True
    assert route.tool_sequence[0].tool == "cross_session_search"


@pytest.mark.asyncio
async def test_route_judge_quant_request_is_marked_as_external_specialized_capability():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="帮我回测一个双均线策略，并给出夏普比率",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search", "web_search"],
    )

    assert route.strategy == "specialized_external_capability"
    assert route.need_tools is False
    assert route.tool_sequence == []


@pytest.mark.asyncio
async def test_route_judge_direct_url_prefers_web_crawl():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="请帮我分析这个网页 https://example.com/news/byd-policy 的内容",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["web_search", "kg_hybrid_search"],
    )

    assert route.strategy == "direct_url_crawl"
    assert route.tool_sequence[0].tool == "web_search"
    assert route.tool_sequence[0].args["urls"] == [
        "https://example.com/news/byd-policy"
    ]


class _CapturingLLMClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def is_available(self):
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


@pytest.mark.asyncio
async def test_route_judge_llm_refinement_uses_selected_prompt_version():
    llm_client = _CapturingLLMClient(
        {
            "need_tools": True,
            "need_memory": False,
            "need_web_search": False,
            "need_path_explanation": True,
            "strategy": "kg_hybrid_first_then_graph_trace",
            "tool_sequence": [
                {"tool": "kg_hybrid_search", "args": {}, "optional": False},
                {"tool": "graph_relation_trace", "args": {}, "optional": False},
            ],
            "reason": "keep relation route",
            "max_iterations": 3,
        }
    )
    judge = RouteJudge(
        llm_client=llm_client,
        default_max_iterations=3,
        prompt_version="v2",
    )

    route = await judge.plan(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        session_context={"history": []},
        user_profile={"industry": "auto"},
        available_capabilities=["kg_hybrid_search", "graph_relation_trace"],
    )

    assert route.strategy == "kg_hybrid_first_then_graph_trace"
    assert judge.prompt_version == "v2"
    assert len(llm_client.calls) == 1
    assert "Prompt version: v2" in llm_client.calls[0]["user_prompt"]
    assert "Refinement policy:" in llm_client.calls[0]["user_prompt"]


def test_route_judge_prompt_version_registry_and_fallback():
    versions = list_route_judge_prompt_versions()

    assert DEFAULT_ROUTE_JUDGE_PROMPT_VERSION in versions
    assert "v2" in versions

    judge = RouteJudge(prompt_version="missing-version")
    assert judge.prompt_version == DEFAULT_ROUTE_JUDGE_PROMPT_VERSION

    system_prompt, user_prompt = build_route_judge_prompt(
        query="supplier update",
        session_context={"history": []},
        available_capabilities=["kg_hybrid_search"],
        current_plan={"strategy": "factual_qa"},
        prompt_version="missing-version",
    )

    assert "route judge" in system_prompt.lower()
    assert f"Prompt version: {DEFAULT_ROUTE_JUDGE_PROMPT_VERSION}" in user_prompt
    assert "Available capabilities:" in user_prompt
