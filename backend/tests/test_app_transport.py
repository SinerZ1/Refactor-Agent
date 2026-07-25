from langgraph.checkpoint.memory import MemorySaver

from agent.workflow import get_checkpointer
from app import is_websocket_origin_allowed


def test_websocket_accepts_dynamic_same_origin_loopback_port():
    assert is_websocket_origin_allowed("http://127.0.0.1:54321", "127.0.0.1:54321")
    assert is_websocket_origin_allowed("http://localhost:49152", "localhost:49152")


def test_websocket_rejects_cross_site_origin_even_with_same_port():
    assert not is_websocket_origin_allowed(
        "https://attacker.example:54321", "127.0.0.1:54321"
    )
    assert not is_websocket_origin_allowed("http://127.0.0.1:54322", "127.0.0.1:54321")


def test_get_checkpointer_no_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


def test_get_checkpointer_invalid_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://invalid-host-name-xyz-123-foo:6379/0")
    saver = get_checkpointer()
    assert isinstance(saver, MemorySaver)


def test_get_checkpointer_valid_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    class FakeRedisClient:
        def ping(self):
            return True

    fake_client = FakeRedisClient()
    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: fake_client)

    class FakeRedisSaver:
        pass

    fake_saver = FakeRedisSaver()
    from langgraph.checkpoint.redis import RedisSaver

    monkeypatch.setattr(
        RedisSaver, "from_conn_string", classmethod(lambda cls, url: fake_saver)
    )

    saver = get_checkpointer()
    assert saver is fake_saver
