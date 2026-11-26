"""服务器 → 代理的指令通道抽象。

阶段 2 的调度器只依赖这个接口；阶段 3 用 WebSocket 实现它（CONNECT / DISCONNECT / RELEASE_ASK，必须 ACK）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Ack:
    ok: bool
    error: str | None = None
    payload: dict = field(default_factory=dict)


class AgentLink(Protocol):
    def is_online(self, agent_id: str) -> bool: ...

    async def send(self, agent_id: str, msg_type: str, payload: dict, *, timeout: float, retries: int) -> Ack:
        """下发指令并等待 ACK；超时按 retries 重试，仍失败返回 Ack(ok=False, error=...)，不抛异常。"""
        ...


class NullLink:
    """没有任何代理在线（阶段 3 的 WebSocket 通道接入前使用）。"""

    def is_online(self, agent_id: str) -> bool:
        return False

    async def send(self, agent_id: str, msg_type: str, payload: dict, *, timeout: float, retries: int) -> Ack:
        return Ack(False, "agent offline")
