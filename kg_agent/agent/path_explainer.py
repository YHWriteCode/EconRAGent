from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kg_agent.agent.prompts import build_path_explainer_prompt


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
RELATION_QUESTION_PATTERN = re.compile(
    r"(为什么|影响|传导|how|why|impact|affect|cause|driv)",
    re.IGNORECASE,
)
SEMANTIC_TAG_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "__cause__",
        ("why", "cause", "reason", "导致", "原因", "为何", "为什么", "传导"),
    ),
    (
        "__impact__",
        ("impact", "affect", "effect", "drive", "推动", "带动", "影响", "拉动"),
    ),
    (
        "__increase__",
        ("increase", "growth", "rise", "surge", "增长", "提升", "扩张", "改善"),
    ),
    (
        "__decrease__",
        ("decrease", "decline", "drop", "fall", "压缩", "下降", "减少", "下滑"),
    ),
    (
        "__policy__",
        ("policy", "regulation", "subsid", "政策", "监管", "补贴"),
    ),
    (
        "__cost__",
        ("cost", "price", "margin", "expense", "成本", "价格", "利润", "毛利"),
    ),
    ("__risk__", ("risk", "pressure", "uncertain", "风险", "压力", "波动")),
)
EVIDENCE_SCORE_THRESHOLD = 3.0
PATH_SCORE_THRESHOLD = 4.0


def _normalize_ascii_token(token: str) -> str:
    lowered = token.lower()
    if lowered.endswith("ies") and len(lowered) > 4:
        return lowered[:-3] + "y"
    for suffix in ("ing", "ed", "ly", "es", "s"):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            return lowered[: -len(suffix)]
    return lowered


def _expand_cjk_token(token: str) -> set[str]:
    cleaned = token.strip()
    if not cleaned:
        return set()
    tokens = {cleaned}
    if len(cleaned) <= 3:
        tokens.update(cleaned)
    span = cleaned[:48]
    for size in (2, 3, 4):
        if len(span) < size:
            break
        for index in range(len(span) - size + 1):
            tokens.add(span[index : index + size])
    return tokens


def _extract_semantic_tags(text: str) -> set[str]:
    lowered = (text or "").lower()
    tags = set()
    for tag, patterns in SEMANTIC_TAG_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            tags.add(tag)
    return tags


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in TOKEN_PATTERN.finditer(text or ""):
        token = match.group(0).strip()
        if not token:
            continue
        if token.isascii():
            tokens.add(token.lower())
            tokens.add(_normalize_ascii_token(token))
        else:
            tokens.update(_expand_cjk_token(token))
    tokens.update(_extract_semantic_tags(text))
    return {token for token in tokens if token}


def _normalized_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1.0, (len(left) * len(right)) ** 0.5)


@dataclass
class ExplainedPath:
    path_text: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class PathExplanation:
    enabled: bool
    question_type: str
    core_entities: list[str]
    paths: list[ExplainedPath]
    final_explanation: str
    uncertainty: str | None = None


class PathExplainer:
    def __init__(self, *, llm_client=None):
        self.llm_client = llm_client

    @staticmethod
    def _is_relation_explanation_query(query: str) -> bool:
        return bool(RELATION_QUESTION_PATTERN.search(query or ""))

    async def explain(
        self,
        query: str,
        graph_paths: list[dict[str, Any]],
        evidence_chunks: list[str],
        domain_schema: dict[str, Any] | None = None,
    ) -> PathExplanation:
        normalized_paths = [
            path
            for path in graph_paths[:3]
            if isinstance(path, dict) and path.get("path_text")
        ]
        if not normalized_paths or not evidence_chunks:
            return self._build_fallback_result(query=query)

        scored_paths = [
            (self._score_path(query, path, evidence_chunks), path)
            for path in normalized_paths
        ]
        scored_paths.sort(key=lambda item: item[0], reverse=True)
        best_score, best_path = scored_paths[0]
        if best_score < PATH_SCORE_THRESHOLD:
            return self._build_fallback_result(query=query)

        evidence = self._select_evidence(best_path, evidence_chunks)
        if not evidence:
            return self._build_fallback_result(query=query)

        explained_path = ExplainedPath(
            path_text=best_path["path_text"],
            nodes=list(best_path.get("nodes", [])),
            edges=list(best_path.get("edges", [])),
            evidence=evidence,
            confidence=min(0.95, 0.35 + min(best_score / 25.0, 0.5)),
        )

        final_explanation = await self._build_final_explanation(
            query=query,
            explained_path=explained_path,
            domain_schema=domain_schema,
        )
        question_type = (
            "relation_explanation"
            if self._is_relation_explanation_query(query)
            else "path_trace"
        )

        core_entities = [
            node.get("id")
            for node in explained_path.nodes
            if isinstance(node, dict) and node.get("id")
        ][:3]
        uncertainty = (
            "Evidence is limited to the retrieved chunks and may require external verification."
            if explained_path.confidence < 0.7
            else None
        )
        return PathExplanation(
            enabled=True,
            question_type=question_type,
            core_entities=core_entities,
            paths=[explained_path],
            final_explanation=final_explanation,
            uncertainty=uncertainty,
        )

    def _score_path(
        self, query: str, path: dict[str, Any], evidence_chunks: list[str]
    ) -> float:
        query_tokens = _tokenize(query)
        path_signals = self._build_path_signals(path)
        evidence_scores = [
            self._score_evidence_chunk(
                query_tokens=query_tokens,
                path_signals=path_signals,
                chunk=chunk,
            )
            for chunk in evidence_chunks
        ]
        best_evidence_score = max(evidence_scores, default=0.0)
        supporting_chunk_count = sum(
            1 for score in evidence_scores if score >= EVIDENCE_SCORE_THRESHOLD
        )
        top_support_scores = sorted(evidence_scores, reverse=True)[:2]
        average_top_support = (
            sum(top_support_scores) / len(top_support_scores)
            if top_support_scores
            else 0.0
        )
        exact_query_mentions = sum(
            1
            for phrase in path_signals["node_phrases"]
            if phrase and phrase in query
        )
        hop_count = max(
            len(path.get("edges", [])),
            max(0, len(path.get("nodes", [])) - 1),
        )
        compact_bonus = 1.0 if 1 <= hop_count <= 3 else 0.0
        hop_penalty = max(0, hop_count - 3) * 1.1
        return (
            _normalized_overlap(query_tokens, path_signals["path_tokens"]) * 8.0
            + _normalized_overlap(query_tokens, path_signals["node_tokens"]) * 7.0
            + _normalized_overlap(query_tokens, path_signals["edge_tokens"]) * 4.0
            + exact_query_mentions * 2.0
            + best_evidence_score * 1.4
            + average_top_support * 0.45
            + supporting_chunk_count * 0.6
            + compact_bonus
            - hop_penalty
        )

    def _select_evidence(
        self, path: dict[str, Any], evidence_chunks: list[str], limit: int = 2
    ) -> list[str]:
        path_signals = self._build_path_signals(path)
        query_tokens = _tokenize(
            " ".join(
                [path.get("path_text", ""), *path_signals["node_phrases"], *path_signals["edge_phrases"]]
            )
        )
        scored: list[tuple[float, str]] = []
        for chunk in evidence_chunks:
            score = self._score_evidence_chunk(
                query_tokens=query_tokens,
                path_signals=path_signals,
                chunk=chunk,
            )
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored or scored[0][0] < EVIDENCE_SCORE_THRESHOLD:
            return []
        return [chunk for _, chunk in scored[:limit]]

    def _build_path_signals(self, path: dict[str, Any]) -> dict[str, Any]:
        path_tokens = _tokenize(path.get("path_text", ""))
        node_tokens: set[str] = set()
        edge_tokens: set[str] = set()
        node_phrases: list[str] = []
        edge_phrases: list[str] = []

        for node in path.get("nodes", []):
            if not isinstance(node, dict):
                continue
            for key in ("id", "name", "label", "entity", "entity_name"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    node_phrases.append(value)
                    node_tokens.update(_tokenize(value))

        for edge in path.get("edges", []):
            if not isinstance(edge, dict):
                continue
            for key in ("relation", "type", "label", "description", "source", "target"):
                value = edge.get(key)
                if isinstance(value, str) and value.strip():
                    edge_phrases.append(value)
                    edge_tokens.update(_tokenize(value))

        path_tokens.update(node_tokens)
        path_tokens.update(edge_tokens)
        return {
            "path_tokens": path_tokens,
            "node_tokens": node_tokens,
            "edge_tokens": edge_tokens,
            "node_phrases": node_phrases,
            "edge_phrases": edge_phrases,
        }

    def _score_evidence_chunk(
        self,
        *,
        query_tokens: set[str],
        path_signals: dict[str, Any],
        chunk: str,
    ) -> float:
        chunk_tokens = _tokenize(chunk)
        if not chunk_tokens:
            return 0.0
        node_mentions = sum(
            1 for phrase in path_signals["node_phrases"] if phrase and phrase in chunk
        )
        edge_mentions = sum(
            1 for phrase in path_signals["edge_phrases"] if phrase and phrase in chunk
        )
        return (
            _normalized_overlap(chunk_tokens, path_signals["path_tokens"]) * 8.0
            + _normalized_overlap(chunk_tokens, query_tokens) * 5.0
            + node_mentions * 2.0
            + edge_mentions * 1.0
        )

    async def _build_final_explanation(
        self,
        *,
        query: str,
        explained_path: ExplainedPath,
        domain_schema: dict[str, Any] | None,
    ) -> str:
        if self.llm_client is not None and self.llm_client.is_available():
            system_prompt, user_prompt = build_path_explainer_prompt(
                query=query,
                graph_paths=[
                    {
                        "path_text": explained_path.path_text,
                        "nodes": explained_path.nodes,
                        "edges": explained_path.edges,
                    }
                ],
                evidence_chunks=explained_path.evidence,
                domain_schema=domain_schema,
            )
            try:
                payload = await self.llm_client.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=500,
                )
                explanation = str(payload.get("final_explanation", "")).strip()
                if explanation:
                    return explanation
            except Exception:
                pass

        evidence_preview = " ".join(explained_path.evidence[:2])
        return (
            f"Based on the graph path '{explained_path.path_text}', "
            f"the strongest supported explanation is that the linked entities and relations "
            f"are reflected in the retrieved evidence: {evidence_preview}"
        )

    @staticmethod
    def _build_fallback_result(query: str) -> PathExplanation:
        question_type = (
            "relation_explanation"
            if PathExplainer._is_relation_explanation_query(query)
            else "path_trace"
        )
        return PathExplanation(
            enabled=False,
            question_type=question_type,
            core_entities=[],
            paths=[],
            final_explanation="",
            uncertainty="No graph path with sufficient supporting evidence was found.",
        )
