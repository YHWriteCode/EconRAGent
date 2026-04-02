KG_HYBRID_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "mode": {"type": "string", "enum": ["hybrid", "mix"]},
        "top_k": {"type": "integer", "minimum": 1},
        "chunk_top_k": {"type": "integer", "minimum": 1},
    },
    "required": ["query"],
}

KG_NAIVE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "top_k": {"type": "integer", "minimum": 1},
        "chunk_top_k": {"type": "integer", "minimum": 1},
    },
    "required": ["query"],
}

GRAPH_ENTITY_LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "entity_name": {"type": "string"},
        "search_limit": {"type": "integer", "minimum": 1},
    },
    "required": ["query"],
}

GRAPH_RELATION_TRACE_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "entity_name": {"type": "string"},
        "max_depth": {"type": "integer", "minimum": 1},
        "max_paths": {"type": "integer", "minimum": 1},
    },
    "required": ["query"],
}

MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "session_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["query", "session_id"],
}

WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "urls": {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
            "minItems": 1,
        },
        "top_k": {"type": "integer", "minimum": 1},
    },
    "required": ["query"],
}

QUANT_BACKTEST_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "symbol": {"type": "string"},
        "strategy_name": {"type": "string"},
    },
    "required": ["query"],
}

KG_INGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "content": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1},
            ],
            "description": "Text or markdown content to ingest into the knowledge graph.",
        },
        "source": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1},
            ],
            "description": "Provenance label (URL, file path, or descriptive tag).",
        },
    },
    "required": ["query", "content"],
}
