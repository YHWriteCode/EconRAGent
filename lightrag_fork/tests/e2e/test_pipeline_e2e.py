from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import time
import traceback
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PARENT = REPO_ROOT.parent
if str(REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(REPO_PARENT))


TEST_TEXT_ECON = (
    "2025年下半年，国内宏观政策强调稳增长与高质量发展并重。"
    "某新能源设备企业在制造业升级中扩大投资，受益于税收优惠与绿色信贷，"
    "订单同比增长。与此同时，消费电子行业在出口回暖与汇率波动影响下出现分化，"
    "头部公司通过供应链数字化降低成本。地方政府发布产业扶持细则，鼓励算力基础设施建设，"
    "并推动银行对中小企业提供更长期限融资，以稳定就业和企业现金流。"
)

TEST_TEXT_A = (
    "A公司在新能源车零部件领域扩产，受益于产业政策与融资成本下降。"
    "行业景气度改善，利润率出现恢复迹象。"
)
TEST_TEXT_B = (
    "B公司聚焦半导体设备国产替代，订单增长来自下游资本开支提升。"
    "公司通过研发投入强化技术壁垒。"
)
TEST_TEXT_C = (
    "C公司在云计算与数据中心业务上调收入指引，受益于AI算力需求扩张。"
    "同时，公司控制费用以提升自由现金流。"
)
TEST_TEXT_DEL = (
    "D公司属于可选消费板块，短期受促销季与库存去化影响。"
    "管理层预计下一季度毛利率边际修复。"
)


@dataclass
class StageSummary:
    name: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failed_items: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failed_items is None:
            self.failed_items = []

    def pass_(self, item: str, detail: str = "") -> None:
        self.passed += 1
        print(f"[PASS] {item}" + (f" - {detail}" if detail else ""))

    def fail(self, item: str, detail: str = "") -> None:
        self.failed += 1
        self.failed_items.append(f"{self.name}/{item}: {detail or 'unknown error'}")
        print(f"[FAIL] {item}" + (f" - {detail}" if detail else ""))

    def skip(self, item: str, detail: str = "") -> None:
        self.skipped += 1
        print(f"[SKIP: {detail or 'not applicable'}] {item}")

    def print_stage_footer(self) -> None:
        print(
            f"===== 阶段汇总({self.name})：{self.passed} passed, "
            f"{self.failed} failed, {self.skipped} skipped ====="
        )


def load_env_files() -> None:
    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(Path(__file__).with_name(".env"), override=False)


def sanitize_endpoint(value: str | None) -> str:
    if not value:
        return "<unset>"
    text = value.strip()
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or "unknown"
        port = parsed.port or "-"
        return f"{parsed.scheme}://{host}:{port}"
    if ":" in text:
        return text
    return text


def parse_host_port(value: str | None, default_port: int | None = None) -> tuple[str, int] | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            return None
        port = parsed.port if parsed.port is not None else default_port
        if port is None:
            return None
        return parsed.hostname, int(port)

    if ":" in text:
        host, port_text = text.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return None

    if default_port is None:
        return None
    return text, int(default_port)


def print_runtime_config() -> None:
    print("===== 当前环境配置（脱敏） =====")
    print(f"REDIS_URI={sanitize_endpoint(os.getenv('REDIS_URI'))}")
    print(f"NEO4J_URI={sanitize_endpoint(os.getenv('NEO4J_URI'))}")
    qdrant_host = os.getenv("QDRANT_HOST")
    qdrant_port = os.getenv("QDRANT_PORT")
    qdrant_url = os.getenv("QDRANT_URL")
    if qdrant_host and qdrant_port:
        print(f"QDRANT={qdrant_host}:{qdrant_port}")
    else:
        print(f"QDRANT_URL={sanitize_endpoint(qdrant_url)}")
    print(f"MONGODB_URI={sanitize_endpoint(os.getenv('MONGODB_URI') or os.getenv('MONGO_URI'))}")
    print(f"LLM_BINDING_HOST={sanitize_endpoint(os.getenv('LLM_BINDING_HOST') or os.getenv('OPENAI_API_BASE'))}")
    print(f"LLM_MODEL_NAME={os.getenv('LLM_MODEL_NAME', '<default>')}")
    print(f"EMBEDDING_MODEL={os.getenv('EMBEDDING_MODEL', '<default>')}")
    print("================================")


def prepare_env_aliases() -> None:
    if not os.getenv("MONGO_URI") and os.getenv("MONGODB_URI"):
        os.environ["MONGO_URI"] = os.environ["MONGODB_URI"]
    if not os.getenv("QDRANT_URL"):
        qdrant_host = os.getenv("QDRANT_HOST")
        qdrant_port = os.getenv("QDRANT_PORT", "6333")
        if qdrant_host:
            os.environ["QDRANT_URL"] = f"http://{qdrant_host}:{qdrant_port}"
    if not os.getenv("OPENAI_API_BASE") and os.getenv("LLM_BINDING_HOST"):
        os.environ["OPENAI_API_BASE"] = os.environ["LLM_BINDING_HOST"]
    if not os.getenv("OPENAI_API_KEY") and os.getenv("LLM_BINDING_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["LLM_BINDING_API_KEY"]


def tcp_check(host: str, port: int, timeout_s: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"{host}:{port}"
    except Exception as e:
        return False, f"{host}:{port} - {e}"


async def stage_connectivity() -> StageSummary:
    summary = StageSummary(name="connectivity")
    print("===== 阶段 0: 环境连通性检查 =====")

    services: list[tuple[str, tuple[str, int] | None, bool]] = []
    services.append(("Redis", parse_host_port(os.getenv("REDIS_URI"), default_port=6379), False))
    services.append(("Neo4j", parse_host_port(os.getenv("NEO4J_URI"), default_port=7687), False))

    qdrant_addr: tuple[str, int] | None = None
    if os.getenv("QDRANT_HOST") and os.getenv("QDRANT_PORT"):
        qdrant_addr = parse_host_port(
            f"{os.getenv('QDRANT_HOST')}:{os.getenv('QDRANT_PORT')}", default_port=6333
        )
    if qdrant_addr is None:
        qdrant_addr = parse_host_port(os.getenv("QDRANT_URL"), default_port=6333)
    services.append(("Qdrant", qdrant_addr, False))

    services.append(
        (
            "MongoDB",
            parse_host_port(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI"), default_port=27017),
            False,
        )
    )
    services.append(
        (
            "LLM API",
            parse_host_port(os.getenv("LLM_BINDING_HOST") or os.getenv("OPENAI_API_BASE"), default_port=80),
            True,
        )
    )

    for name, addr, optional in services:
        if addr is None:
            if optional:
                summary.skip(name, "未配置该服务地址")
            else:
                summary.fail(name, "地址未配置")
            continue
        ok, detail = tcp_check(addr[0], addr[1], timeout_s=2.0)
        if ok:
            print(f"[OK] {name} - {detail}")
            summary.pass_(name, detail)
        else:
            print(f"[FAIL: {detail}] {name}")
            summary.fail(name, detail)

    summary.print_stage_footer()
    return summary


def build_local_lock_backend():
    from lightrag_fork.kg.lock_backend import LocalLockBackend

    locks: dict[str, asyncio.Lock] = {}

    class _Ctx:
        def __init__(self, lock: asyncio.Lock):
            self._lock = lock

        async def __aenter__(self):
            await self._lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self._lock.locked():
                self._lock.release()

    def factory(namespace: str, keys: list[str], enable_logging: bool):
        del enable_logging
        key = f"{namespace}:{'|'.join(sorted(keys))}"
        return _Ctx(locks.setdefault(key, asyncio.Lock()))

    return LocalLockBackend(local_context_factory=factory)


async def stage_redis_lock() -> StageSummary:
    summary = StageSummary(name="redis_lock")
    print("===== 阶段 1: Redis 分布式锁基础验证 =====")

    from lightrag_fork.kg.lock_backend import LockBackendUnavailable, LockLease
    from lightrag_fork.kg.redis_lock_backend import RedisLockBackend, RedisLockManager, RedisLockOptions

    redis_uri = os.getenv("REDIS_URI")
    if not redis_uri:
        summary.fail("Redis URI", "REDIS_URI 未配置")
        summary.print_stage_footer()
        return summary

    prefix = os.getenv("LIGHTRAG_LOCK_KEY_PREFIX", "lightrag:e2e:lock")
    owner = f"e2e:{int(time.time())}"
    backend = RedisLockBackend(redis_url=redis_uri, key_prefix=prefix, fail_mode="strict", fallback_backend=None)

    item = "acquire + release 基础闭环"
    lease_basic = None
    try:
        lease_basic = await backend.acquire("e2e:basic", owner, 5, 0, 0.05, False)
        assert lease_basic is not None
        assert await backend.is_locked("e2e:basic")
        released = await backend.release(lease_basic)
        assert released is True
        assert not await backend.is_locked("e2e:basic")
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))
        if lease_basic is not None:
            try:
                await backend.release(lease_basic)
            except Exception:
                pass

    item = "token 防误释放"
    lease_real = None
    try:
        lease_real = await backend.acquire("e2e:token", owner, 5, 0, 0.05, False)
        assert lease_real is not None
        forged = LockLease(
            key=lease_real.key,
            token="forged",
            owner_id="attacker",
            acquired_at=time.time(),
            ttl_s=5,
            backend="redis",
            backend_data={"redis_key": lease_real.backend_data["redis_key"], "value": '{"token":"forged","owner":"attacker","ts":0}'},
        )
        released_by_forged = await backend.release(forged)
        still_locked = await backend.is_locked("e2e:token")
        assert released_by_forged is False
        assert still_locked is True
        await backend.release(lease_real)
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))
        if lease_real is not None:
            try:
                await backend.release(lease_real)
            except Exception:
                pass

    item = "non-blocking acquire（已持有时立即返回 False/None）"
    lease_holder = None
    try:
        lease_holder = await backend.acquire("e2e:nonblocking", owner, 5, 0, 0.05, False)
        assert lease_holder is not None
        start = time.perf_counter()
        second = await backend.acquire("e2e:nonblocking", f"{owner}:2", 5, 0, 0.05, False)
        elapsed = time.perf_counter() - start
        assert second is None
        assert elapsed < 0.5
        summary.pass_(item, f"elapsed={elapsed:.4f}s")
    except Exception as e:
        summary.fail(item, str(e))
    finally:
        if lease_holder is not None:
            try:
                await backend.release(lease_holder)
            except Exception:
                pass

    item = "TTL 自动过期后可重新获取"
    lease_ttl = None
    lease_after = None
    try:
        lease_ttl = await backend.acquire("e2e:ttl", owner, 1, 0, 0.05, False)
        assert lease_ttl is not None
        await asyncio.sleep(1.3)
        lease_after = await backend.acquire("e2e:ttl", f"{owner}:after", 5, 0, 0.05, False)
        assert lease_after is not None
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))
    finally:
        if lease_after is not None:
            try:
                await backend.release(lease_after)
            except Exception:
                pass
        elif lease_ttl is not None:
            try:
                await backend.release(lease_ttl)
            except Exception:
                pass

    item = "renew 将 TTL 延长"
    lease_renew = None
    try:
        lease_renew = await backend.acquire("e2e:renew", owner, 2, 0, 0.05, False)
        assert lease_renew is not None
        client = await backend.manager._get_client()  # type: ignore[attr-defined]
        redis_key = lease_renew.backend_data["redis_key"]
        await asyncio.sleep(0.8)
        ttl_before = await client.pttl(redis_key)
        renewed = await backend.renew(lease_renew, ttl_s=4)
        ttl_after = await client.pttl(redis_key)
        assert renewed is True
        assert ttl_after > ttl_before
        summary.pass_(item, f"pttl_before={ttl_before}, pttl_after={ttl_after}")
    except Exception as e:
        summary.fail(item, str(e))
    finally:
        if lease_renew is not None:
            try:
                await backend.release(lease_renew)
            except Exception:
                pass

    item = "多 key 获取失败时已获取 key 回滚"
    held_lease = None
    try:
        manager: RedisLockManager = backend.manager
        options = RedisLockOptions(ttl_s=5, wait_timeout_s=0, retry_interval_s=0.05, auto_renew=False)
        held_lease = await manager.acquire("e2e:multi:b", owner=f"{owner}:held", options=options)
        assert held_lease is not None
        leases = await manager.acquire_many(["e2e:multi:a", "e2e:multi:b"], owner=f"{owner}:many", options=options)
        key_a_locked = await manager.is_locked("e2e:multi:a")
        assert leases is None
        assert key_a_locked is False
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))
    finally:
        if held_lease is not None:
            try:
                await backend.release(held_lease)
            except Exception:
                pass

    item = "strict 模式下 Redis 不可用抛异常"
    try:
        strict_bad = RedisLockBackend(
            redis_url="redis://127.0.0.1:1/0?socket_connect_timeout=0.2&socket_timeout=0.2",
            key_prefix=prefix,
            fail_mode="strict",
            fallback_backend=None,
        )
        got_expected = False
        try:
            await strict_bad.acquire("e2e:strict:unavailable", owner, 3, 0, 0.05, False)
        except LockBackendUnavailable:
            got_expected = True
        except Exception:
            got_expected = True
        assert got_expected is True
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))

    item = "fallback_local 模式下 Redis 不可用降级本地锁"
    try:
        local_backend = build_local_lock_backend()
        fallback_bad = RedisLockBackend(
            redis_url="redis://127.0.0.1:1/0?socket_connect_timeout=0.2&socket_timeout=0.2",
            key_prefix=prefix,
            fail_mode="fallback_local",
            fallback_backend=local_backend,
        )
        lease_local = await fallback_bad.acquire("e2e:fallback", owner, 3, 0, 0.05, False)
        assert lease_local is not None
        assert lease_local.backend == "local"
        released = await fallback_bad.release(lease_local)
        assert released is True
        summary.pass_(item)
    except Exception as e:
        summary.fail(item, str(e))

    summary.print_stage_footer()
    return summary


def _require_env(summary: StageSummary, keys: list[str]) -> bool:
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        summary.fail("环境变量检查", f"缺少: {', '.join(missing)}")
        return False
    return True


def _build_embedding_func(llm_base_url: str | None, llm_api_key: str | None, embedding_model: str | None):
    from dataclasses import replace
    from lightrag_fork.llm.openai import openai_embed

    kwargs: dict[str, Any] = {}
    if llm_base_url:
        kwargs["base_url"] = llm_base_url
    if llm_api_key:
        kwargs["api_key"] = llm_api_key
    if embedding_model:
        kwargs["model"] = embedding_model

    if kwargs:
        return replace(openai_embed, func=partial(openai_embed.func, **kwargs))
    return openai_embed


async def _cleanup_workspace_data(rag) -> None:
    storages = [
        rag.text_chunks,
        rag.full_docs,
        rag.full_entities,
        rag.full_relations,
        rag.entity_chunks,
        rag.relation_chunks,
        rag.entities_vdb,
        rag.relationships_vdb,
        rag.chunks_vdb,
        rag.chunk_entity_relation_graph,
        rag.doc_status,
    ]
    for storage in storages:
        if storage is not None:
            try:
                await storage.drop()
            except Exception:
                pass


async def _wait_doc_processed(rag, doc_id: str, timeout_s: float = 180.0) -> dict[str, Any]:
    start = time.time()
    while True:
        doc = await rag.doc_status.get_by_id(doc_id)
        if doc and str(doc.get("status", "")).lower() == "processed":
            return doc
        if time.time() - start > timeout_s:
            raise TimeoutError(f"Document {doc_id} not processed within {timeout_s}s")
        await asyncio.sleep(0.5)


async def stage_graph_vector() -> StageSummary:
    summary = StageSummary(name="graph_vector")
    print("===== 阶段 2: 基础图与向量操作验证 =====")

    required = [
        "REDIS_URI",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "QDRANT_URL",
        "MONGO_URI",
        "MONGO_DATABASE",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
    ]
    if not _require_env(summary, required):
        summary.print_stage_footer()
        return summary

    try:
        from lightrag_fork import LightRAG, QueryParam
        from lightrag_fork.llm.openai import openai_complete_if_cache
    except Exception as e:
        summary.fail("导入 LightRAG/LLM 组件", str(e))
        summary.print_stage_footer()
        return summary

    workspace = f"e2e_graph_vector_{int(time.time())}"
    llm_base_url = os.getenv("OPENAI_API_BASE")
    llm_api_key = os.getenv("OPENAI_API_KEY")
    llm_model_name = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    embedding_model = os.getenv("EMBEDDING_MODEL")

    rag = None
    doc_id = f"doc-e2e-{int(time.time())}"

    try:
        rag = LightRAG(
            working_dir=str(REPO_ROOT / "tests" / "e2e" / "rag_storage"),
            workspace=workspace,
            kv_storage="RedisKVStorage",
            vector_storage="QdrantVectorDBStorage",
            graph_storage="Neo4JStorage",
            doc_status_storage="MongoDocStatusStorage",
            llm_model_func=openai_complete_if_cache,
            llm_model_name=llm_model_name,
            llm_model_kwargs={"base_url": llm_base_url, "api_key": llm_api_key},
            embedding_func=_build_embedding_func(llm_base_url, llm_api_key, embedding_model),
        )
        await rag.initialize_storages()
        summary.pass_("LightRAG 初始化（initialize_storages）")
    except Exception as e:
        summary.fail("LightRAG 初始化（initialize_storages）", str(e))
        summary.print_stage_footer()
        return summary

    try:
        await rag.ainsert(TEST_TEXT_ECON, ids=[doc_id], file_paths=["e2e_stage2.txt"])
        summary.pass_("插入经济领域测试文本")

        doc_status_obj = await _wait_doc_processed(rag, doc_id, timeout_s=180)
        summary.pass_("等待 pipeline 处理完成")

        nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
        if len(nodes) >= 1:
            summary.pass_("Neo4j 至少存在 1 个实体节点", f"nodes={len(nodes)}")
        else:
            summary.fail("Neo4j 至少存在 1 个实体节点", "nodes=0")

        entity_hits = await rag.entities_vdb.query("宏观政策", top_k=3)
        if entity_hits:
            summary.pass_("Qdrant entities_vdb 有向量结果", f"hits={len(entity_hits)}")
        else:
            summary.fail("Qdrant entities_vdb 有向量结果", "hits=0")

        chunk_hits = await rag.chunks_vdb.query("宏观政策", top_k=3)
        if chunk_hits:
            summary.pass_("Qdrant chunks_vdb 有向量结果", f"hits={len(chunk_hits)}")
        else:
            summary.fail("Qdrant chunks_vdb 有向量结果", "hits=0")

        if str(doc_status_obj.get("status", "")).lower() == "processed":
            summary.pass_("doc_status 为 processed")
        else:
            summary.fail("doc_status 为 processed", str(doc_status_obj))

        naive_resp = await rag.aquery("文中提到了哪些公司和政策工具？", QueryParam(mode="naive"))
        if isinstance(naive_resp, str) and naive_resp.strip():
            summary.pass_("naive 查询返回非空")
        else:
            summary.fail("naive 查询返回非空", "response is empty")

        hybrid_resp = await rag.aquery("请总结文本中的行业变化与政策影响。", QueryParam(mode="hybrid"))
        if isinstance(hybrid_resp, str) and hybrid_resp.strip():
            summary.pass_("hybrid 查询返回非空")
        else:
            summary.fail("hybrid 查询返回非空", "response is empty")

    except Exception as e:
        summary.fail("阶段执行", f"{e}\n{traceback.format_exc()}")
    finally:
        if rag is not None:
            try:
                await _cleanup_workspace_data(rag)
                summary.pass_("测试数据清理（workspace 隔离）")
            except Exception as e:
                summary.fail("测试数据清理（workspace 隔离）", str(e))
            try:
                await rag.finalize_storages()
            except Exception:
                pass

    summary.print_stage_footer()
    return summary


async def stage_concurrent() -> StageSummary:
    summary = StageSummary(name="concurrent")
    print("===== 阶段 3: 并发锁 + pipeline 集成验证 =====")

    required = [
        "REDIS_URI",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "QDRANT_URL",
        "MONGO_URI",
        "MONGO_DATABASE",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
    ]
    if not _require_env(summary, required):
        summary.print_stage_footer()
        return summary

    try:
        from lightrag_fork import LightRAG
        from lightrag_fork.base import DocStatus
        from lightrag_fork.kg.shared_storage import get_namespace_data, get_pipeline_runtime_lock
        from lightrag_fork.llm.openai import openai_complete_if_cache
    except Exception as e:
        summary.fail("导入并发测试组件", str(e))
        summary.print_stage_footer()
        return summary

    workspace = f"e2e_concurrent_{int(time.time())}"
    llm_base_url = os.getenv("OPENAI_API_BASE")
    llm_api_key = os.getenv("OPENAI_API_KEY")
    llm_model_name = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    embedding_model = os.getenv("EMBEDDING_MODEL")
    doc_id_a = f"doc-a-{workspace}"
    doc_id_b = f"doc-b-{workspace}"
    doc_id_c = f"doc-c-{workspace}"
    doc_id_del = f"doc-del-{workspace}"

    rag1 = None
    rag2 = None

    try:
        rag1 = LightRAG(
            working_dir=str(REPO_ROOT / "tests" / "e2e" / "rag_storage"),
            workspace=workspace,
            kv_storage="RedisKVStorage",
            vector_storage="QdrantVectorDBStorage",
            graph_storage="Neo4JStorage",
            doc_status_storage="MongoDocStatusStorage",
            llm_model_func=openai_complete_if_cache,
            llm_model_name=llm_model_name,
            llm_model_kwargs={"base_url": llm_base_url, "api_key": llm_api_key},
            embedding_func=_build_embedding_func(llm_base_url, llm_api_key, embedding_model),
        )
        rag2 = LightRAG(
            working_dir=str(REPO_ROOT / "tests" / "e2e" / "rag_storage"),
            workspace=workspace,
            kv_storage="RedisKVStorage",
            vector_storage="QdrantVectorDBStorage",
            graph_storage="Neo4JStorage",
            doc_status_storage="MongoDocStatusStorage",
            llm_model_func=openai_complete_if_cache,
            llm_model_name=llm_model_name,
            llm_model_kwargs={"base_url": llm_base_url, "api_key": llm_api_key},
            embedding_func=_build_embedding_func(llm_base_url, llm_api_key, embedding_model),
        )
        await rag1.initialize_storages()
        await rag2.initialize_storages()
        summary.pass_("双实例初始化成功")
    except Exception as e:
        summary.fail("双实例初始化成功", str(e))
        summary.print_stage_footer()
        return summary

    pending_seen = False
    runtime_busy_seen = False
    stop_watch = asyncio.Event()

    async def watch_pipeline_flags():
        nonlocal pending_seen
        while not stop_watch.is_set():
            try:
                pipeline_status = await get_namespace_data("pipeline_status", workspace=workspace)
                if pipeline_status.get("request_pending", False):
                    pending_seen = True
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def probe_runtime_busy():
        nonlocal runtime_busy_seen
        for _ in range(80):
            if stop_watch.is_set():
                return
            ctx = get_pipeline_runtime_lock(workspace=workspace, wait_timeout_s=0)
            try:
                await ctx.__aenter__()
            except TimeoutError:
                runtime_busy_seen = True
                return
            else:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            await asyncio.sleep(0.05)

    watcher_task = asyncio.create_task(watch_pipeline_flags())
    probe_task = asyncio.create_task(probe_runtime_busy())

    try:
        await asyncio.gather(
            rag1.ainsert(TEST_TEXT_A, ids=[doc_id_a], file_paths=["e2e_a.txt"]),
            rag2.ainsert(TEST_TEXT_B, ids=[doc_id_b], file_paths=["e2e_b.txt"]),
        )
        summary.pass_("并发 ainsert 执行完成")

        if runtime_busy_seen:
            summary.pass_("同 workspace 并发时仅一个 pipeline 持有 runtime 锁")
        else:
            summary.fail("同 workspace 并发时仅一个 pipeline 持有 runtime 锁", "未观察到 runtime lock contention")

        if pending_seen:
            summary.pass_("request_pending 在并发竞争时被置位")
        else:
            summary.fail("request_pending 在并发竞争时被置位", "未观察到 request_pending=True")

        processed_docs = await rag1.doc_status.get_docs_by_status(DocStatus.PROCESSED)
        has_a = doc_id_a in processed_docs
        has_b = doc_id_b in processed_docs
        if has_a and has_b:
            summary.pass_("并发插入后文档状态正确（processed）")
        else:
            summary.fail("并发插入后文档状态正确（processed）", f"doc_a={has_a}, doc_b={has_b}")

        await rag1.ainsert(TEST_TEXT_DEL, ids=[doc_id_del], file_paths=["e2e_del.txt"])
        summary.pass_("删除冲突测试预置文档成功")

        insert_task = asyncio.create_task(rag1.ainsert(TEST_TEXT_C, ids=[doc_id_c], file_paths=["e2e_c.txt"]))
        await asyncio.sleep(0.2)
        delete_task = asyncio.create_task(rag2.adelete_by_doc_id(doc_id_del))
        insert_result, delete_result = await asyncio.wait_for(asyncio.gather(insert_task, delete_task), timeout=240)
        del insert_result
        if getattr(delete_result, "status", None) in {"success", "not_found", "not_allowed", "fail"}:
            summary.pass_("删除与处理并发无死锁", f"delete_status={getattr(delete_result, 'status', 'unknown')}")
        else:
            summary.fail("删除与处理并发无死锁", f"unexpected delete result: {delete_result}")

    except asyncio.TimeoutError:
        summary.fail("并发阶段执行", "任务超时，疑似死锁")
    except Exception as e:
        summary.fail("并发阶段执行", f"{e}\n{traceback.format_exc()}")
    finally:
        stop_watch.set()
        for t in (watcher_task, probe_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass

        for rag in (rag1, rag2):
            if rag is None:
                continue
            try:
                await _cleanup_workspace_data(rag)
            except Exception:
                pass
            try:
                await rag.finalize_storages()
            except Exception:
                pass

    summary.print_stage_footer()
    return summary


def resolve_stages(stage_args: list[str]) -> list[str]:
    all_stages = ["connectivity", "redis_lock", "graph_vector", "concurrent"]
    picked = all_stages if "all" in stage_args else stage_args
    seen: set[str] = set()
    ordered: list[str] = []
    for st in picked:
        if st not in seen:
            seen.add(st)
            ordered.append(st)
    return ordered


async def run(selected_stages: list[str]) -> int:
    load_env_files()
    prepare_env_aliases()
    print_runtime_config()

    stage_handlers = {
        "connectivity": stage_connectivity,
        "redis_lock": stage_redis_lock,
        "graph_vector": stage_graph_vector,
        "concurrent": stage_concurrent,
    }

    results: list[StageSummary] = []
    for stage in selected_stages:
        results.append(await stage_handlers[stage]())

    total_pass = sum(s.passed for s in results)
    total_fail = sum(s.failed for s in results)
    total_skip = sum(s.skipped for s in results)
    all_failed_items = [item for s in results for item in (s.failed_items or [])]

    print("===== 全量汇总 =====")
    print(f"TOTAL: {total_pass} passed, {total_fail} failed, {total_skip} skipped")
    if total_fail == 0 and total_pass > 0:
        print("总体评级：ALL PASS → Ready for next module")
        return 0
    if total_fail > 0 and total_pass > 0:
        print("总体评级：PARTIAL PASS → 列出失败项，阻塞开发")
        for item in all_failed_items:
            print(f"- {item}")
        return 1
    print("总体评级：ALL FAIL → 环境未就绪")
    for item in all_failed_items:
        print(f"- {item}")
    return 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LightRAG E2E pipeline checker")
    parser.add_argument(
        "--stage",
        nargs="+",
        required=True,
        choices=["connectivity", "redis_lock", "graph_vector", "concurrent", "all"],
        help="选择执行阶段，可传多个",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    stages = resolve_stages(args.stage)
    code = asyncio.run(run(stages))
    raise SystemExit(code)


if __name__ == "__main__":
    main()

