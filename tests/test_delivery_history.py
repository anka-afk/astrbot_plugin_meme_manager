from astrbot_plugin_meme_manager.backend import delivery_history
from astrbot_plugin_meme_manager.backend.delivery_history import DeliveryHistory


def test_history_is_bounded_and_separated_by_conversation():
    history = DeliveryHistory(max_sessions=2, max_records=2)
    history.record("a", {"category": "happy"})
    history.record("a", {"category": "sad"})
    history.record("a", {"category": "calm"})
    assert history.recent("a") == [{"category": "sad"}, {"category": "calm"}]
    history.record("b", {"category": "hello"})
    history.record("c", {"category": "bye"})
    assert history.recent("a") == []
    assert history.recent("b") == [{"category": "hello"}]
    assert history.recent("unknown") == []


def test_history_expiration_and_returned_records_do_not_mutate_storage(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(delivery_history.time, "monotonic", lambda: clock[0])
    history = DeliveryHistory(ttl=10)
    history.record("a", {"category": "happy"})
    history.recent("a")[0]["category"] = "changed"
    assert history.recent("a")[0]["category"] == "happy"
    clock[0] = 12
    assert history.recent("a") == []
    history.record("a", {"category": "hello"})
    assert history.recent("a") == [{"category": "hello"}]
