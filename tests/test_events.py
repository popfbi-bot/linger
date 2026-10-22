"""事件类型：流式载体必须自带前端与 trace 页面要的全部字段（docs §5.3）。"""

from __future__ import annotations

from linger.server import ServerEvent


def test_event_types_are_the_four_documented_ones():
    event = ServerEvent(type="thinking", data={"stage": "recall"})
    assert event.type == "thinking"
    assert ServerEvent(type="delta", data={"text": "嗯"}).type == "delta"
    assert ServerEvent(type="affect", data={"label": "低落"}).type == "affect"
    assert ServerEvent(type="done", data={}).type == "done"


def test_data_defaults_to_empty_dict():
    assert ServerEvent(type="delta").data == {}
    # 默认值不能是共享的可变对象
    a, b = ServerEvent(type="delta"), ServerEvent(type="delta")
    a.data["x"] = 1
    assert b.data == {}


def test_done_event_carries_turn_id_used_memories_state_usage():
    done = ServerEvent.done(
        turn_id=42,
        used_memories=[{"id": "m_1", "text": "面试砸了", "layer": 2, "score": 0.71}],
        state={"affect": {"label": "低落"}, "relation": {"level": 3, "intimacy": 64.2}},
        usage={"in": 3204, "out": 86, "cached": 1200},
    )
    assert done.type == "done"
    assert set(done.data) == {"turn_id", "used_memories", "state", "usage"}
    assert done.data["turn_id"] == 42
    assert done.data["used_memories"][0]["id"] == "m_1"
    assert done.data["state"]["relation"]["level"] == 3
    assert done.data["usage"]["in"] == 3204


def test_affect_event_exposes_label_and_pad():
    affect = ServerEvent.affect("低落", {"pleasure": -0.28, "arousal": 0.31, "dominance": 0.4}, 3)
    assert affect.data["label"] == "低落"
    assert affect.data["pleasure"] == -0.28
    assert affect.data["relation_level"] == 3


def test_delta_event_is_plain_text_increment():
    assert ServerEvent.delta("砸到").data == {"text": "砸到"}
