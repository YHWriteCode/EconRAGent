import pytest

from kg_agent.memory.conversation_memory import ConversationMemoryStore


@pytest.mark.asyncio
async def test_conversation_memory_appends_and_returns_recent_history():
    store = ConversationMemoryStore()
    await store.append_message("s1", "user", "比亚迪是什么公司")
    await store.append_message("s1", "assistant", "比亚迪是一家新能源汽车公司")
    await store.append_message("s1", "user", "继续刚才的话题")

    history = await store.get_recent_history("s1", turns=2)

    assert len(history) == 3
    assert history[-1]["content"] == "继续刚才的话题"


@pytest.mark.asyncio
async def test_conversation_memory_search_returns_relevant_messages():
    store = ConversationMemoryStore()
    await store.append_message("s1", "user", "讨论一下比亚迪和新能源汽车行业")
    await store.append_message("s1", "assistant", "比亚迪在新能源汽车行业中份额较高")
    await store.append_message("s1", "user", "再说说油价")

    matches = await store.search("s1", "比亚迪行业", limit=2)

    assert len(matches) >= 1
    assert "比亚迪" in matches[0]["content"]
