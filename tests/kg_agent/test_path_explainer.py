import pytest

from kg_agent.agent.path_explainer import PathExplainer


@pytest.mark.asyncio
async def test_path_explainer_returns_best_path_with_evidence():
    explainer = PathExplainer()
    graph_paths = [
        {
            "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪",
            "nodes": [{"id": "政策支持"}, {"id": "新能源汽车行业"}, {"id": "比亚迪"}],
            "edges": [{"source": "政策支持", "target": "新能源汽车行业"}],
        },
        {
            "path_text": "锂价 -> 成本压力 -> 比亚迪",
            "nodes": [{"id": "锂价"}, {"id": "成本压力"}, {"id": "比亚迪"}],
            "edges": [{"source": "锂价", "target": "成本压力"}],
        },
    ]
    evidence = [
        "政策支持推动了新能源汽车行业扩张，并带动比亚迪订单增长。",
        "锂价波动会影响电池材料成本。",
    ]

    result = await explainer.explain(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        graph_paths=graph_paths,
        evidence_chunks=evidence,
    )

    assert result.enabled is True
    assert len(result.paths) == 1
    assert "比亚迪" in result.final_explanation


@pytest.mark.asyncio
async def test_path_explainer_falls_back_without_evidence():
    explainer = PathExplainer()

    result = await explainer.explain(
        query="为什么会这样？",
        graph_paths=[{"path_text": "A -> B", "nodes": [], "edges": []}],
        evidence_chunks=[],
    )

    assert result.enabled is False
    assert result.paths == []
