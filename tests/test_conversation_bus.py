"""ConversationBus 测试 — 覆盖消息发送/接收/共识/Handoff/搜索

测试数据原则：所有消息内容使用中性词汇，
不包含任何业务领域术语（如编剧、分镜、漫画等）。
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock


class TestConversationBusInit:
    """初始化测试"""

    def test_create_bus(self, bus):
        assert bus.project_id == "test_proj_001"
        assert bus.history == []
        assert bus._agents == {}

    def test_register_agent(self, bus):
        bus.register("agent_1", "处理节点 A", "🔧")
        agent = bus.get_agent("agent_1")
        assert agent is not None
        assert agent["name"] == "处理节点 A"
        assert agent["emoji"] == "🔧"

    def test_register_agent_default_emoji(self, bus):
        bus.register("agent_x", "TestAgent")
        assert bus.get_agent("agent_x")["emoji"] == "🤖"

    def test_get_nonexistent_agent(self, bus):
        assert bus.get_agent("no_such") is None

    def test_list_agents_empty(self, bus):
        assert bus.list_agents() == []

    def test_list_agents_after_register(self, bus):
        bus.register("a1", "A")
        bus.register("a2", "B")
        agents = bus.list_agents()
        assert len(agents) == 2


@pytest.mark.asyncio
class TestConversationBusSend:
    """消息发送与持久化测试"""

    async def test_send_status(self, bus_with_messages):
        # send_status 在 fixture 中已调用
        assert len(bus_with_messages.history) >= 1
        status_msgs = bus_with_messages.get_by_type("status")
        assert len(status_msgs) >= 1
        assert "系统启动" in status_msgs[0].content

    async def test_send_task(self, bus):
        msg = await bus.send_task("worker_a", "worker_b", "请处理子任务")
        assert msg is not None
        assert msg.message_type == "task"
        assert msg.sender_id == "worker_a"
        assert msg.receiver_id == "worker_b"
        assert "子任务" in msg.content

    async def test_send_handoff(self, bus):
        msg = await bus.send_handoff("worker_a", "worker_b", "交接完成")
        assert msg.message_type == "handoff"
        assert msg.id is not None  # 持久化后应有 ID

    async def test_send_question_and_answer(self, bus):
        q = await bus.send_question("worker_b", "worker_a", "如何处理异常？")
        assert q.message_type == "question"

        a = await bus.send_answer("worker_a", "worker_b", "重试三次后放弃", parent_id=q.id)
        assert a.message_type == "answer"
        assert a.parent_id == q.id

    async def test_send_escalate(self, bus):
        msg = await bus.send_escalate("agent_a", "需要选择方案", options=["方案A", "方案B"])
        assert msg is not None
        assert len(msg.metadata.get("options") or []) >= 2

    async def test_send_alert(self, bus):
        msg = await bus.send_alert("资源不足", level="error")
        assert msg.message_type == "alert"
        assert msg.metadata.get("level") == "error"

    async def test_send_thinking(self, bus):
        msg = await bus.send_thinking("agent_1", "正在分析输入...")
        assert msg.message_type == "thinking"

    async def test_history_grows(self, bus):
        initial_len = len(bus.history)
        await bus.send_status("test1")
        await bus.send_status("test2")
        assert len(bus.history) == initial_len + 2


class TestConversationBusQuery:
    """查询方法测试"""

    def test_get_history_all(self, bus):
        h = bus.get_history()
        assert h == []

    def test_get_by_sender(self, bus_with_messages):
        msgs = bus_with_messages.get_by_sender("agent_a")
        assert len(msgs) >= 2  # task + handoff

    def test_get_by_type(self, bus_with_messages):
        statuses = bus_with_messages.get_by_type("status")
        assert len(statuses) >= 1
        handoffs = bus_with_messages.get_by_type("handoff")
        assert len(handoffs) >= 1

    def test_get_stats(self, bus_with_messages):
        stats = bus_with_messages.get_stats()
        assert stats["total"] >= 3
        assert "by_type" in stats
        assert "by_sender" in stats

    def test_clear(self, bus_with_messages):
        assert len(bus_with_messages.history) > 0
        bus_with_messages.clear()
        assert bus_with_messages.history == []


@pytest.mark.asyncio
class TestHandoffPayload:
    """Handoff 结构化查询测试"""

    async def test_send_handoff_payload(self, bus):
        from core.communication.handoff import HandoffPayload

        payload = HandoffPayload(
            from_agent="worker_a",
            to_agent="worker_b",
            phase="phase_1",
            summary="处理阶段已完成",
            result={"items_processed": 5},
            instructions="请基于此结果继续下一阶段",
        )
        msg = await bus.send_handoff_payload(payload)
        assert msg is not None
        assert payload.message_id == msg.id

    def test_get_handoffs_for(self, bus_with_messages):
        handoffs = bus_with_messages.get_handoffs_for("agent_b")
        assert len(handoffs) >= 1
        h = handoffs[0]
        assert h.from_agent == "agent_a"
        assert h.to_agent == "agent_b"

    def test_get_latest_handoff_for(self, bus_with_messages):
        latest = bus_with_messages.get_latest_handoff_for("agent_b")
        assert latest is not None

    def test_get_handoffs_for_empty(self, bus):
        assert bus.get_handoffs_for("nobody") == []


class TestConsensus:
    """共识检测测试"""

    def test_consensus_reached_same_text(self, bus):
        messages = [
            self._make_chat_msg("a", "方案A最优"),
            self._make_chat_msg("b", "方案A最优"),
        ]
        result = bus.has_consensus(messages, threshold=0.5)
        assert result["reached"] is True
        assert result["confidence"] > 0.8

    def test_consensus_not_reached_different(self, bus):
        messages = [
            self._make_chat_msg("a", "选择方案A因为简单"),
            self._make_chat_msg("b", "选择方案B因为扩展性好"),
        ]
        result = bus.has_consensus(messages, threshold=0.9)
        assert result["reached"] is False

    def test_consensus_needs_two_agents(self, bus):
        messages = [self._make_chat_msg("a", "only me")]
        result = bus.has_consensus(messages)
        assert result["reached"] is False
        assert result["confidence"] == 0

    def test_consensus_chinese_tokenization(self, bus):
        """中文分词的 Jaccard 相似度"""
        messages = [
            self._make_chat_msg("a", "这个方案可行"),
            self._make_chat_msg("b", "这个方案确实可行"),
        ]
        result = bus.has_consensus(messages, threshold=0.3)
        assert result["confidence"] > 0

    @staticmethod
    def _make_chat_msg(sender: str, content: str):
        from core.agent.types import ConversationMessage
        return ConversationMessage(
            project_id="test",
            sender_type="agent",
            sender_id=sender,
            message_type="chat",
            content=content,
        )


class TestEscalationToDecision:
    """分歧升级为决策卡测试"""

    def test_basic_escalation(self, bus):
        messages = [
            self._make_chat_msg("a", "应该用策略 X"),
            self._make_chat_msg("b", "应该用策略 Y"),
        ]
        card = bus.escalate_to_decision(messages)
        assert card is not None
        assert card.question != ""
        assert len(card.options) >= 2

    def test_escalation_excludes_system(self, bus):
        messages = [
            self._make_chat_msg("system", "系统提示"),
            self._make_chat_msg("a", "选项 A"),
        ]
        card = bus.escalate_to_decision(messages)
        agent_ids = [o.agent_id for o in card.options]
        assert "system" not in agent_ids

    def test_escalation_empty_messages(self, bus):
        """空讨论升级时回退到系统兜底选项（继续讨论 / 终止等）"""
        card = bus.escalate_to_decision([])
        assert card is not None
        assert len(card.options) >= 2
        assert all(o.agent_id == "" for o in card.options)

    @staticmethod
    def _make_chat_msg(sender: str, content: str):
        from core.agent.types import ConversationMessage
        return ConversationMessage(
            project_id="test",
            sender_type="agent",
            sender_id=sender,
            message_type="chat",
            content=content,
        )


class TestPendingEscalations:
    """待处理升级查询测试"""

    async def test_no_pending_when_answered(self, bus):
        esc = await bus.send_escalate("agent_a", "需要决策", options=["A", "B"])
        await bus.send_decide(option="A", parent_id=esc.id)
        pending = bus.get_pending_escalations()
        assert len(pending) == 0

    async def test_pending_when_unanswered(self, bus):
        await bus.send_escalate("agent_a", "需要决策", options=["A", "B"])
        pending = bus.get_pending_escalations()
        assert len(pending) == 1


class TestUnansweredQuestions:
    """未回答问题查询测试"""

    async def test_answered_question_not_listed(self, bus):
        q = await bus.send_question("a", "b", "问题？")
        await bus.send_answer("b", "a", "答案！", parent_id=q.id)
        unanswered = bus.get_unanswered_questions("b")
        assert len(unanswered) == 0

    async def test_unanswered_listed(self, bus):
        await bus.send_question("a", "b", "未回答的问题")
        unanswered = bus.get_unanswered_questions("b")
        assert len(unanswered) == 1


class TestTokenization:
    """中英文分词器测试"""

    def test_pure_english(self, bus):
        tokens = bus._tokenize("Hello World Test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_pure_chinese(self, bus):
        tokens = bus._tokenize("你好世界测试")
        assert "你" in tokens
        assert "好" in tokens
        assert len(tokens) == 6

    def test_mixed_lang(self, bus):
        tokens = bus._tokenize("Hello世界")
        assert "hello" in tokens
        assert "世" in tokens

    def test_empty_string(self, bus):
        assert bus._tokenize("") == []
        assert bus._tokenize(None) == []
