import pytest

from kg_agent.agent.path_explainer import PathExplainer, _tokenize


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
    assert result.paths[0].path_text == "政策支持 -> 新能源汽车行业 -> 比亚迪"
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


def test_tokenize_adds_cjk_entity_tokens_and_relation_tags():
    tokens = _tokenize("政策变化如何影响比亚迪利润表现？")

    assert "比亚迪" in tokens
    assert "利润" in tokens
    assert "__impact__" in tokens
    assert "__policy__" in tokens


@pytest.mark.asyncio
async def test_path_explainer_prefers_semantically_supported_path():
    explainer = PathExplainer()
    graph_paths = [
        {
            "path_text": "政策调整 -> 新能源汽车行业 -> 比亚迪订单",
            "nodes": [{"id": "政策调整"}, {"id": "新能源汽车行业"}, {"id": "比亚迪订单"}],
            "edges": [{"source": "政策调整", "target": "新能源汽车行业"}],
        },
        {
            "path_text": "比亚迪 -> 海外市场 -> 表现",
            "nodes": [{"id": "比亚迪"}, {"id": "海外市场"}, {"id": "表现"}],
            "edges": [{"source": "比亚迪", "target": "海外市场"}],
        },
    ]
    evidence = [
        "政策调整推动新能源汽车行业需求扩张，并带动比亚迪订单改善。",
        "比亚迪海外市场表现稳定。",
    ]

    result = await explainer.explain(
        query="政策变化如何影响比亚迪订单增长？",
        graph_paths=graph_paths,
        evidence_chunks=evidence,
    )

    assert result.enabled is True
    assert result.paths[0].path_text == "政策调整 -> 新能源汽车行业 -> 比亚迪订单"
    assert result.paths[0].evidence[0] == evidence[0]


def test_path_explainer_penalizes_overlong_paths():
    explainer = PathExplainer()
    evidence = ["锂价上涨推高电池材料成本，压缩比亚迪利润空间。"]
    short_path = {
        "path_text": "锂价上涨 -> 成本压力 -> 比亚迪利润",
        "nodes": [{"id": "锂价上涨"}, {"id": "成本压力"}, {"id": "比亚迪利润"}],
        "edges": [{"source": "锂价上涨", "target": "成本压力"}],
    }
    long_path = {
        "path_text": "锂价上涨 -> 上游资源供给 -> 中游材料厂 -> 电池成本 -> 整车毛利 -> 比亚迪利润",
        "nodes": [
            {"id": "锂价上涨"},
            {"id": "上游资源供给"},
            {"id": "中游材料厂"},
            {"id": "电池成本"},
            {"id": "整车毛利"},
            {"id": "比亚迪利润"},
        ],
        "edges": [
            {"source": "锂价上涨", "target": "上游资源供给"},
            {"source": "上游资源供给", "target": "中游材料厂"},
            {"source": "中游材料厂", "target": "电池成本"},
            {"source": "电池成本", "target": "整车毛利"},
            {"source": "整车毛利", "target": "比亚迪利润"},
        ],
    }

    assert explainer._score_path(
        "锂价上涨如何影响比亚迪利润？", short_path, evidence
    ) > explainer._score_path("锂价上涨如何影响比亚迪利润？", long_path, evidence)
