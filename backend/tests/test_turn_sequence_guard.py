from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.nodes import (
    call_architect,
    call_developer,
    call_reviewer,
    ensure_valid_turn_sequence,
)


def test_ensure_valid_turn_sequence_appends_human_message_when_ending_with_ai():
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hello"),
        AIMessage(content="ai response"),
    ]
    cleaned = ensure_valid_turn_sequence(messages, fallback_prompt="Please continue")
    assert len(cleaned) == 4
    assert isinstance(cleaned[-1], HumanMessage)
    assert cleaned[-1].content == "Please continue"


def test_ensure_valid_turn_sequence_keeps_messages_if_ending_with_human():
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hello"),
    ]
    cleaned = ensure_valid_turn_sequence(messages)
    assert len(cleaned) == 2
    assert isinstance(cleaned[-1], HumanMessage)


def test_developer_node_sanitizes_messages_before_invoking_llm(monkeypatch):
    captured_messages = []

    class DummyLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content="Developer response")

    monkeypatch.setattr("agent.nodes.get_llm_from_config", lambda cfg: DummyLLM())

    # State 中最后一条消息是 Architect 生成的 AIMessage
    state = {
        "messages": [
            HumanMessage(content="重构代码"),
            AIMessage(content="【架构评估】拆分函数"),
        ]
    }

    call_developer(state, config={})

    # 验证传入 llm.invoke 的消息列表最后一条绝不能是 AIMessage
    assert len(captured_messages) > 0
    assert not isinstance(captured_messages[-1], AIMessage)
    assert isinstance(captured_messages[-1], HumanMessage)


def test_architect_node_sanitizes_messages_before_invoking_llm(monkeypatch):
    captured_messages = []

    class DummyLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content="Architect response")

    monkeypatch.setattr("agent.nodes.get_llm_from_config", lambda cfg: DummyLLM())

    state = {
        "messages": [
            HumanMessage(content="初始需求"),
            AIMessage(content="已有的 AI 总结"),
        ]
    }

    result = call_architect(state, config={})

    assert len(captured_messages) > 0
    assert not isinstance(captured_messages[-1], AIMessage)
    assert isinstance(captured_messages[-1], HumanMessage)
    assert result["refactor_plan"] is None
    assert "默认任务图" in result["plan_error"]


def test_reviewer_node_sanitizes_messages_before_invoking_llm(monkeypatch):
    captured_messages = []

    class DummyLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content="Reviewer response")

    monkeypatch.setattr("agent.nodes.get_llm_from_config", lambda cfg: DummyLLM())

    state = {
        "messages": [
            HumanMessage(content="初始需求"),
            AIMessage(content="Developer 输出的代码"),
        ],
        "change_records": [],
    }

    call_reviewer(state, config={})

    assert len(captured_messages) > 0
    assert not isinstance(captured_messages[-1], AIMessage)
    assert isinstance(captured_messages[-1], HumanMessage)
