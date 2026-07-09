from dataclasses import dataclass


@dataclass
class _FakeBrainResult:
    title: str
    path: str
    chunk: str
    score: float = 1.0
    reason: str = "keyword"
    citation: str = "03-projects/restia.md#L1"


class _FakeBrainService:
    def __init__(self):
        self.captured = []
        self.results = [
            _FakeBrainResult(
                title="Restia",
                path="03-projects/restia.md",
                chunk="Restia keeps Markdown canonical.",
            )
        ]

    def search(self, query, limit=8):
        return self.results[:limit]

    def capture(self, text, source="api", approved=False):
        self.captured.append({"text": text, "source": source, "approved": approved})
        return "04-decisions/restia.md"


def _manager(tmp_path, owner="admin"):
    from pathlib import Path

    from src.brain_memory import BrainAugmentedMemoryManager, BrainMemoryConfig

    config = BrainMemoryConfig(
        root=Path("/tmp/brain"),
        vault_path=Path("/tmp/brain/vault"),
        db_path=Path("/tmp/brain/data/brain.sqlite"),
        owner=owner,
    )
    service = _FakeBrainService()
    return BrainAugmentedMemoryManager(str(tmp_path), config, service), service


def test_brain_recall_is_owner_gated(tmp_path):
    manager, _service = _manager(tmp_path, owner="admin")

    assert manager.get_external_relevant_memories("markdown", owner="other") == []

    hits = manager.get_external_relevant_memories("markdown", owner="admin")
    assert len(hits) == 1
    assert hits[0]["source"] == "restia_brain"
    assert hits[0]["metadata"]["citation"] == "03-projects/restia.md#L1"


def test_brain_write_sync_marks_native_entry(tmp_path):
    manager, service = _manager(tmp_path)
    entry = manager.add_entry("Restia should use Restia Brain.", owner="admin")

    manager.save([entry])
    stored = manager.load(owner="admin")

    assert service.captured == [
        {
            "text": "Restia should use Restia Brain.",
            "source": "odysseus:user:admin",
            "approved": True,
        }
    ]
    assert stored[0]["metadata"]["brain_path"] == "04-decisions/restia.md"


def test_load_merges_brain_rows_but_save_keeps_cache_native(tmp_path):
    manager, _service = _manager(tmp_path)
    brain_row = {
        "id": "brain:1",
        "text": "Brain fact",
        "source": "restia_brain",
        "readonly": True,
        "owner": "admin",
    }
    manager.list_external_memories = lambda owner=None, max_items=1000: [brain_row]

    native = manager.add_entry("Native fact", owner="admin")
    manager.save([native])

    loaded = manager.load(owner="admin")
    assert [row["id"] for row in loaded] == [native["id"], "brain:1"]

    loaded.append(manager.add_entry("Another native fact", owner="admin"))
    manager.save(loaded)

    cached = manager.load_all()
    assert [row["text"] for row in cached] == ["Native fact", "Another native fact"]
    assert all(row.get("source") != "restia_brain" for row in cached)


def test_duplicate_detection_sees_brain_rows(tmp_path):
    manager, _service = _manager(tmp_path)
    manager.list_external_memories = lambda owner=None, max_items=1000: [
        {
            "id": "brain:1",
            "text": "Restia keeps Markdown canonical.",
            "source": "restia_brain",
            "readonly": True,
            "owner": owner,
        }
    ]

    assert manager.find_duplicates("restia keeps markdown canonical.")[0]["id"] == "brain:1"


def test_chat_processor_injects_external_brain_memory_without_native_rows():
    from src.chat_processor import ChatProcessor

    class Memory:
        def load(self, owner=None):
            return []

        def get_external_relevant_memories(self, query, owner=None, max_items=3):
            return [
                {
                    "id": "brain:1",
                    "text": "Restia keeps Markdown canonical.",
                    "source": "restia_brain",
                    "category": "brain",
                    "metadata": {"citation": "03-projects/restia.md#L1"},
                }
            ]

    class Docs:
        rag_manager = None

    processor = ChatProcessor(Memory(), Docs())
    preface, _rag_sources, _web_sources = processor.build_context_preface(
        "What does Restia use?",
        session=None,
        use_rag=False,
        use_memory=True,
        owner="admin",
    )

    joined = "\n".join(message["content"] for message in preface)
    assert "Restia keeps Markdown canonical." in joined
    assert "03-projects/restia.md#L1" in joined


def test_memory_search_route_merges_external_brain_results(monkeypatch):
    from unittest.mock import MagicMock

    import routes.memory_routes as memory_routes

    class Memory:
        def load(self, owner=None):
            return []

        def get_relevant_memories(self, query, memories, threshold=0.05, max_items=20):
            return []

        def get_external_relevant_memories(self, query, owner=None, max_items=20):
            return [{"id": "brain:1", "text": "Brain fact", "source": "restia_brain"}]

    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "admin")
    router = memory_routes.setup_memory_routes(Memory(), MagicMock())
    search = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/search" and "POST" in route.methods
    )

    result = search(request=None, query="brain", session_id=None, category=None)

    assert result["memories"] == [{"id": "brain:1", "text": "Brain fact", "source": "restia_brain"}]


def test_memory_get_route_merges_readonly_brain_list(monkeypatch):
    from unittest.mock import MagicMock

    import routes.memory_routes as memory_routes

    class Memory:
        def load(self, owner=None):
            return [{"id": "native:1", "text": "Native fact", "owner": owner}]

        def list_external_memories(self, owner=None, max_items=100):
            return [
                {
                    "id": "brain:1",
                    "text": "Brain fact",
                    "source": "restia_brain",
                    "readonly": True,
                    "owner": owner,
                }
            ]

    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "admin")
    router = memory_routes.setup_memory_routes(Memory(), MagicMock())
    get_memory = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory" and "GET" in route.methods
    )

    result = get_memory(request=None)

    assert result["memory"] == [
        {"id": "native:1", "text": "Native fact", "owner": "admin"},
        {
            "id": "brain:1",
            "text": "Brain fact",
            "source": "restia_brain",
            "readonly": True,
            "owner": "admin",
        },
    ]
