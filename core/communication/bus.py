"""ConversationBus — Agent 通信总线

职责：
- 7 种核心消息类型路由（task | handoff | review_request | question | answer | escalate | decide）
- 扩展类型（status | thinking | alert | chat）
- 多方讨论 & 共识检测（Jaccard 相似度）
- 并行竞争
- 分歧升级为决策卡
- 持久化到 SQLite + FTS5 全文搜索
"""

from __future__ import annotations
import asyncio
import json
import time
from typing import Optional, List, Dict, Any
from core.storage.database import get_db
from core.agent.types import (
    ConversationMessage, DecisionCard, DecisionOption, MessageType,
)
from core.communication.handoff import HandoffPayload


class ConversationBus:
    """Agent 通信总线 — 所有 Agent 间消息的中枢"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.history: List[ConversationMessage] = []  # 内存缓存
        self._agents: Dict[str, dict] = {}  # 已注册的 Agent

    # ==================== Agent 注册 ====================

    def register(self, agent_id: str, agent_name: str, agent_emoji: str = "🤖", channels: list = None):
        """注册 Agent 到通信总线"""
        self._agents[agent_id] = {
            "id": agent_id,
            "name": agent_name,
            "emoji": agent_emoji,
            "channels": channels or ["*"],
            "registered_at": self._now(),
        }

    def get_agent(self, agent_id: str) -> Optional[dict]:
        return self._agents.get(agent_id)

    def list_agents(self) -> List[dict]:
        return list(self._agents.values())

    # ==================== 发送消息 ====================

    async def send(self, msg: ConversationMessage) -> ConversationMessage:
        """发送一条消息并持久化"""
        db = await get_db()
        cursor = await db.execute("""
            INSERT INTO conversations
                (project_id, phase, sender_type, sender_id, receiver_id,
                 message_type, content, parent_id, metadata, consensus_reached, created_at)
            VALUES (:project_id, :phase, :sender_type, :sender_id, :receiver_id,
                    :message_type, :content, :parent_id, :metadata, :consensus_reached, :created_at)
        """, {
            "project_id": self.project_id,
            "phase": msg.phase or "",
            "sender_type": msg.sender_type,
            "sender_id": msg.sender_id,
            "receiver_id": msg.receiver_id,
            "message_type": msg.message_type,
            "content": msg.content,
            "parent_id": msg.parent_id,
            "metadata": json.dumps(msg.metadata, ensure_ascii=False) if msg.metadata else None,
            "consensus_reached": 1 if msg.consensus_reached else 0,
            "created_at": self._now(),
        })
        await db.commit()

        msg.id = cursor.lastrowid
        msg.created_at = self._now()

        # FTS5 索引
        try:
            await db.execute(
                "INSERT INTO conversations_fts(rowid, content) VALUES (?, ?)",
                (msg.id, msg.content)
            )
            await db.commit()
        except Exception:
            pass  # FTS5 可能不存在

        self.history.append(msg)
        self._broadcast_to_sse(msg)
        return msg

    def _broadcast_to_sse(self, msg: ConversationMessage):
        """将 Bus 消息桥接到 SSE EventBus（监控面板实时可见）"""
        if msg.message_type not in (
            MessageType.STATUS, MessageType.THINKING, MessageType.ALERT,
            MessageType.CHAT, MessageType.HANDOFF, MessageType.ESCALATE,
            MessageType.ANSWER, MessageType.QUESTION, MessageType.REVIEW_REQUEST,
        ):
            return
        try:
            from core.events import event_bus
            event_bus.broadcast(
                event="agent_message",
                data={
                    "message_type": msg.message_type,
                    "sender_type": msg.sender_type,
                    "sender_id": msg.sender_id or "",
                    "receiver_id": msg.receiver_id or "",
                    "content": msg.content,
                    "phase": msg.phase or "",
                    "metadata": msg.metadata,
                    "id": msg.id,
                },
                project_id=self.project_id,
            )
        except Exception:
            pass

    # ==================== 快捷发送方法 ====================

    async def send_task(self, from_agent: str, to_agent: str, content: str, metadata: dict = None):
        """Agent → Agent：委派任务"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id=to_agent,
            message_type=MessageType.TASK, content=content, metadata=metadata,
        ))

    async def send_handoff(self, from_agent: str, to_agent: str, content: str, metadata: dict = None):
        """Agent → Agent：工作交接"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id=to_agent,
            message_type=MessageType.HANDOFF, content=content, metadata=metadata,
        ))

    async def send_handoff_payload(self, payload: HandoffPayload) -> ConversationMessage:
        """发送结构化 handoff（含完整 metadata）"""
        msg = await self.send(ConversationMessage(
            project_id=self.project_id,
            phase=payload.phase,
            sender_type="agent",
            sender_id=payload.from_agent,
            receiver_id=payload.to_agent,
            message_type=MessageType.HANDOFF,
            content=payload.to_context_text(),
            metadata=payload.to_metadata(),
        ))
        payload.message_id = msg.id
        return msg

    async def send_review_request(self, from_agent: str, to_agent: str, content: str, metadata: dict = None):
        """Agent → Agent：请求评审"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id=to_agent,
            message_type=MessageType.REVIEW_REQUEST, content=content, metadata=metadata,
        ))

    async def send_question(self, from_agent: str, to_agent: str, question: str, metadata: dict = None):
        """Agent → Agent：询问信息"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id=to_agent,
            message_type=MessageType.QUESTION, content=question, metadata=metadata,
        ))

    async def send_answer(
        self,
        from_agent: str,
        to_agent: str,
        answer: str,
        parent_id: int = None,
        metadata: dict = None,
    ):
        """Agent → Agent：回复"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id=to_agent,
            message_type=MessageType.ANSWER, content=answer, parent_id=parent_id,
            metadata=metadata,
        ))

    async def send_escalate(self, from_agent: str, question: str, options: list = None, metadata: dict = None):
        """Agent → User：分歧升级为决策"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent, receiver_id="user",
            message_type=MessageType.ESCALATE, content=question,
            metadata={"options": options or [], **(metadata or {})},
        ))

    async def send_decide(self, option: str, parent_id: int = None):
        """User → Agent：提交决策"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="user", sender_id="user",
            message_type=MessageType.DECIDE, content=option, parent_id=parent_id,
        ))

    async def send_status(self, content: str):
        """系统状态广播"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="system", message_type=MessageType.STATUS, content=content,
        ))

    async def send_alert(self, content: str, level: str = "warning"):
        """系统警告"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="system", message_type=MessageType.ALERT, content=content,
            metadata={"level": level},
        ))

    async def send_thinking(self, agent_id: str, content: str, metadata: dict = None):
        """Agent 思考过程广播"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=agent_id,
            message_type=MessageType.THINKING, content=content, metadata=metadata,
        ))

    async def broadcast(self, msg_type: str, content: str, sender_id: str = "system"):
        """广播消息"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent" if sender_id != "system" else "system",
            sender_id=sender_id,
            message_type=msg_type, content=content,
        ))

    async def send_chat(self, from_agent: str, content: str, metadata: dict = None):
        """Agent 发言（讨论中）"""
        return await self.send(ConversationMessage(
            project_id=self.project_id,
            sender_type="agent", sender_id=from_agent,
            message_type=MessageType.CHAT, content=content, metadata=metadata,
        ))

    # ==================== 问答闭环 ====================

    async def wait_for_answer(self, question_id: int, from_agent: str, timeout: float = 30) -> Optional[ConversationMessage]:
        """轮询等待指定 Agent 对指定问题的回答

        Args:
            question_id: 问题的消息 ID
            from_agent: 期望回答者
            timeout: 超时秒数（默认 30）

        Returns:
            找到的 answer 消息，或超时返回 None
        """
        start = time.time()
        while time.time() - start < timeout:
            for msg in self.history:
                if (msg.message_type == MessageType.ANSWER
                        and msg.parent_id == question_id
                        and msg.sender_id == from_agent):
                    return msg
            await asyncio.sleep(0.5)
        return None

    def get_unanswered_questions(self, agent_id: str) -> list:
        """获取指定 Agent 收到的未回答问题"""
        answered_ids = {
            m.parent_id for m in self.history
            if getattr(m, "message_type", "") == MessageType.ANSWER
            and getattr(m, "sender_id", "") == agent_id
        }
        return [
            m for m in self.history
            if getattr(m, "message_type", "") == MessageType.QUESTION
            and getattr(m, "receiver_id", "") == agent_id
            and getattr(m, "id", None) not in answered_ids
        ]

    # ==================== Handoff 查询 ====================

    def get_latest_handoff_for(self, agent_id: str) -> Optional[HandoffPayload]:
        """获取指定 Agent 收到的最新 handoff"""
        handoffs = self.get_handoffs_for(agent_id)
        return handoffs[-1] if handoffs else None

    def get_handoffs_for(self, agent_id: str) -> List[HandoffPayload]:
        """获取指定 Agent 收到的所有 handoff（按时间序）"""
        result = []
        for msg in self.history:
            if msg.message_type != MessageType.HANDOFF:
                continue
            if msg.receiver_id != agent_id:
                continue
            meta = msg.metadata or {}
            if meta.get("handoff_version"):
                result.append(HandoffPayload(
                    from_agent=meta.get("from_agent", msg.sender_id),
                    to_agent=meta.get("to_agent", agent_id),
                    phase=meta.get("phase", msg.phase or ""),
                    summary=meta.get("summary", msg.content[:200]),
                    result=meta.get("result", {}),
                    instructions=meta.get("instructions", ""),
                    artifact_keys=meta.get("artifact_keys", []),
                    message_id=msg.id,
                ))
            else:
                result.append(HandoffPayload(
                    from_agent=msg.sender_id,
                    to_agent=agent_id,
                    summary=msg.content[:200],
                    message_id=msg.id,
                ))
        return result

    # ==================== 讨论 & 共识 ====================

    def _extract_last_statements(self, messages: list) -> Dict[str, str]:
        """提取每个 Agent 的最新发言"""
        last_statements: Dict[str, str] = {}
        for msg in messages:
            if hasattr(msg, "sender_id"):
                sender_id = msg.sender_id
                content = msg.content
            elif isinstance(msg, dict):
                sender_id = msg.get("sender_id", "")
                content = msg.get("content", "")
            else:
                continue
            if sender_id and sender_id != "system":
                last_statements[sender_id] = content
        return last_statements

    async def has_consensus_async(
        self,
        messages: list,
        topic: str = "",
        threshold: float = 0.7,
        use_llm: bool = True,
        agent_names: Dict[str, str] = None,
    ) -> dict:
        """共识检测 — 优先 LLM，回退 Jaccard"""
        statements = self._extract_last_statements(messages)
        if len(statements) < 2:
            return {"reached": False, "confidence": 0, "consensus": None, "method": "none"}

        if use_llm:
            from core.communication.consensus import check_consensus_llm
            llm_result = await check_consensus_llm(topic, statements, threshold, agent_names)
            if llm_result is not None:
                if llm_result.get("reached") and not llm_result.get("consensus"):
                    llm_result["consensus"] = self._extract_consensus(list(statements.values()))
                return llm_result

        jaccard = self.has_consensus(messages, threshold)
        jaccard["method"] = "jaccard"
        return jaccard

    def has_consensus(self, messages: list, threshold: float = 0.7) -> dict:
        """
        检查讨论是否达成共识
        - 提取每个 Agent 的最后一条消息
        - 计算两两 Jaccard 词级相似度
        - 全部 > threshold 则认为共识
        """
        if len(messages) < 2:
            return {"reached": False, "confidence": 0, "consensus": None}

        # 按发送者分组，取最后一条
        last_statements = self._extract_last_statements(messages)

        agents = list(last_statements.items())
        if len(agents) < 2:
            return {"reached": False, "confidence": 0, "consensus": None}

        # 两两 Jaccard 相似度
        total_sim = 0
        pairs = 0
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                w1 = set(self._tokenize(agents[i][1]))
                w2 = set(self._tokenize(agents[j][1]))
                inter = len(w1 & w2)
                union = len(w1 | w2)
                total_sim += inter / union if union > 0 else 0
                pairs += 1

        avg_sim = total_sim / pairs if pairs > 0 else 0
        reached = avg_sim >= threshold

        return {
            "reached": reached,
            "confidence": round(avg_sim, 2),
            "consensus": self._extract_consensus([a[1] for a in agents]) if reached else None,
        }

    def escalate_to_decision(self, messages: list) -> DecisionCard:
        """分歧升级为决策卡"""
        positions: Dict[str, dict] = {}
        for msg in messages:
            # 兼容 Pydantic model 和 dict
            if hasattr(msg, "sender_id"):
                sender_id = msg.sender_id or ""
                content = msg.content or ""
            elif isinstance(msg, dict):
                sender_id = msg.get("sender_id", "")
                content = msg.get("content", "")
            else:
                continue
            if sender_id and sender_id != "system":
                positions[sender_id] = {"agent_id": sender_id, "content": content}

        options = [
            DecisionOption(
                id=f"opt_{i+1}",
                label=pos["content"][:60] + ("..." if len(pos["content"]) > 60 else ""),
                agent_id=pos["agent_id"],
                agent_view=pos["content"],
            )
            for i, pos in enumerate(positions.values())
        ]
        from core.communication.decision_defaults import DEFAULT_DECISION_QUESTION, ensure_decision_options

        options = ensure_decision_options(options)
        question = DEFAULT_DECISION_QUESTION
        for msg in reversed(messages or []):
            sender = getattr(msg, "sender_id", None) or (msg.get("sender_id") if isinstance(msg, dict) else "")
            mtype = getattr(msg, "message_type", None) or (msg.get("message_type") if isinstance(msg, dict) else "")
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else "")
            if sender and sender != "system" and mtype in ("chat", MessageType.CHAT) and content:
                question = f"{DEFAULT_DECISION_QUESTION}（参考：{str(content)[:120]}）"
                break

        return DecisionCard(
            project_id=self.project_id,
            question=question,
            options=options,
        )

    # ==================== 查询 ====================

    def get_history(self, since: str = None) -> list:
        if since:
            return [m for m in self.history if m.created_at and m.created_at > since]
        return list(self.history)

    def get_by_sender(self, sender_id: str) -> list:
        return [m for m in self.history if m.sender_id == sender_id]

    def get_by_type(self, message_type: str) -> list:
        return [m for m in self.history if m.message_type == message_type]

    def get_by_phase(self, phase: str) -> list:
        return [m for m in self.history if m.phase == phase]

    def get_history_between(self, agent_a: str, agent_b: str) -> list:
        return [
            m for m in self.history
            if (m.sender_id == agent_a and m.receiver_id == agent_b)
            or (m.sender_id == agent_b and m.receiver_id == agent_a)
        ]

    def get_pending_escalations(self) -> list:
        """获取未处理的 escalations"""
        return [
            m for m in self.history
            if m.message_type == MessageType.ESCALATE
            and not any(
                r.parent_id == m.id and r.message_type == MessageType.DECIDE
                for r in self.history
            )
        ]

    def get_stats(self) -> dict:
        stats = {"total": len(self.history), "by_type": {}, "by_sender": {}, "consensus": 0, "pending_escalations": 0}
        for msg in self.history:
            stats["by_type"][msg.message_type] = stats["by_type"].get(msg.message_type, 0) + 1
            stats["by_sender"][msg.sender_id or msg.sender_type] = stats["by_sender"].get(msg.sender_id or msg.sender_type, 0) + 1
            if msg.consensus_reached:
                stats["consensus"] += 1
        stats["pending_escalations"] = len(self.get_pending_escalations())
        return stats

    async def search(self, query: str) -> list:
        """FTS5 搜索对话内容"""
        db = await get_db()
        try:
            cursor = await db.execute("""
                SELECT c.* FROM conversations c
                JOIN conversations_fts fts ON c.rowid = fts.rowid
                WHERE conversations_fts MATCH ?
                ORDER BY rank LIMIT 50
            """, (query,))
            rows = await cursor.fetchall()
            return [self._row_to_message(dict(r)) for r in rows]
        except Exception:
            cursor = await db.execute(
                "SELECT * FROM conversations WHERE content LIKE ? AND project_id = ? ORDER BY created_at DESC LIMIT 50",
                (f"%{query}%", self.project_id)
            )
            return [self._row_to_message(dict(r)) for r in await cursor.fetchall()]

    async def load_history(self):
        """从 DB 加载历史"""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE project_id = ? ORDER BY created_at ASC",
            (self.project_id,)
        )
        rows = await cursor.fetchall()
        self.history = [self._row_to_message(dict(r)) for r in rows]
        return self.history

    def clear(self):
        self.history = []

    # ==================== 内部工具 ====================

    def _tokenize(self, text: str) -> list:
        """中英文分词"""
        tokens = []
        buf = ""
        for ch in text or "":
            if "\u4e00" <= ch <= "\u9fff":
                if buf:
                    tokens.append(buf.lower())
                    buf = ""
                tokens.append(ch)
            elif ch.isalnum():
                buf += ch
            else:
                if buf:
                    tokens.append(buf.lower())
                    buf = ""
        if buf:
            tokens.append(buf.lower())
        return tokens

    def _extract_consensus(self, statements: list) -> str:
        """提炼共识关键词"""
        all_words = []
        for s in statements:
            all_words.extend(self._tokenize(s))
        freq: Dict[str, int] = {}
        for w in all_words:
            if len(w) > 1:
                freq[w] = freq.get(w, 0) + 1
        top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]
        return f"共识关键点: {', '.join(w for w, _ in top)}" if top else "达成基本共识"

    def _row_to_message(self, row: dict) -> ConversationMessage:
        return ConversationMessage(
            id=row.get("id"),
            project_id=row.get("project_id", ""),
            phase=row.get("phase", ""),
            sender_type=row.get("sender_type", "system"),
            sender_id=row.get("sender_id", ""),
            receiver_id=row.get("receiver_id", ""),
            message_type=row.get("message_type", "chat"),
            content=row.get("content", ""),
            parent_id=row.get("parent_id"),
            metadata=json.loads(row["metadata"]) if row.get("metadata") else None,
            consensus_reached=bool(row.get("consensus_reached")),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _now() -> str:
        from datetime import datetime
        return datetime.now().isoformat()
