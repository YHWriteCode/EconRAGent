from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kg_agent.agent.prompts import build_path_explainer_prompt


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_PATTERN.finditer(text or "")}


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
        if best_score <= 0:
            return self._build_fallback_result(query=query)

        evidence = self._select_evidence(best_path, evidence_chunks)
        if not evidence:
            return self._build_fallback_result(query=query)

        explained_path = ExplainedPath(
            path_text=best_path["path_text"],
            nodes=list(best_path.get("nodes", [])),
            edges=list(best_path.get("edges", [])),
            evidence=evidence,
            confidence=min(0.95, 0.45 + min(best_score / 10.0, 0.45)),
        )

        final_explanation = await self._build_final_explanation(
            query=query,
            explained_path=explained_path,
            domain_schema=domain_schema,
        )
        question_type = "relation_explanation" if re.search(
            r"(为什么|影响|传导|why|impact|affect)", query, re.IGNORECASE
        ) else "path_trace"

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
    ) -> int:
        query_tokens = _tokenize(query)
        path_text = str(path.get("path_text", ""))
        path_tokens = _tokenize(path_text)
        overlap = len(query_tokens & path_tokens)
        evidence_overlap = 0
        for chunk in evidence_chunks:
            evidence_overlap = max(evidence_overlap, len(path_tokens & _tokenize(chunk)))
        node_bonus = 1 if 2 <= len(path.get("nodes", [])) <= 4 else 0
        return overlap + evidence_overlap + node_bonus

    def _select_evidence(
        self, path: dict[str, Any], evidence_chunks: list[str], limit: int = 2
    ) -> list[str]:
        path_tokens = _tokenize(path.get("path_text", ""))
        scored: list[tuple[int, str]] = []
        for chunk in evidence_chunks:
            score = len(path_tokens & _tokenize(chunk))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

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
        question_type = "relation_explanation" if re.search(
            r"(为什么|影响|传导|why|impact|affect)", query, re.IGNORECASE
        ) else "path_trace"
        return PathExplanation(
            enabled=False,
            question_type=question_type,
            core_entities=[],
            paths=[],
            final_explanation="",
            uncertainty="No graph path with sufficient supporting evidence was found.",
        )
