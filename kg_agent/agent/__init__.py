__all__ = [
    "AgentCore",
    "CapabilityDefinition",
    "CapabilityRegistry",
    "PathExplanation",
    "PathExplainer",
    "RouteDecision",
    "RouteJudge",
    "ToolRegistry",
]


def __getattr__(name: str):
    if name == "AgentCore":
        from .agent_core import AgentCore

        return AgentCore
    if name in {"CapabilityDefinition", "CapabilityRegistry"}:
        from .capability_registry import CapabilityDefinition, CapabilityRegistry

        return {
            "CapabilityDefinition": CapabilityDefinition,
            "CapabilityRegistry": CapabilityRegistry,
        }[name]
    if name in {"PathExplanation", "PathExplainer"}:
        from .path_explainer import PathExplanation, PathExplainer

        return {
            "PathExplanation": PathExplanation,
            "PathExplainer": PathExplainer,
        }[name]
    if name in {"RouteDecision", "RouteJudge"}:
        from .route_judge import RouteDecision, RouteJudge

        return {
            "RouteDecision": RouteDecision,
            "RouteJudge": RouteJudge,
        }[name]
    if name == "ToolRegistry":
        from .tool_registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
