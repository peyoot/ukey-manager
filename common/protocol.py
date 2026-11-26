"""服务器 ⇄ 代理的消息定义（pydantic）。传输：wss://<server>:8443/ws/agent，每条消息一个 JSON 文本帧。

信封：{"id": "uuid", "type": "CONNECT", "ts": 1763452800, "payload": {...}}

认证流程：
  1. 新代理 ENROLL（带注册令牌）→ 管理员批准（或 IP 命中 auto_approve_ips）→ 服务器在 ACK 里下发代理专属 token（只发一次）
  2. 之后每次连接先 REGISTER（带专属 token）；服务器 ACK 后推送 CONFIG
规则：服务器指令必须 ACK；同 id 重发 = 重试（接收方按 id 去重）；ACK 通过 ref_id 关联。
"""
from __future__ import annotations

import json
import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

MAX_MESSAGE_BYTES = 64 * 1024


class MsgType(str, Enum):
    ENROLL = "ENROLL"          # 代理→服务器
    REGISTER = "REGISTER"      # 代理→服务器
    CONFIG = "CONFIG"          # 服务器→代理
    HEARTBEAT = "HEARTBEAT"    # 代理→服务器
    REQUEST = "REQUEST"        # 代理→服务器
    RELEASE = "RELEASE"        # 代理→服务器
    EVENT = "EVENT"            # 代理→服务器
    CONNECT = "CONNECT"        # 服务器→代理
    DISCONNECT = "DISCONNECT"  # 服务器→代理
    RELEASE_ASK = "RELEASE_ASK"  # 服务器→代理
    NOTIFY = "NOTIFY"          # 服务器→代理
    RUN_SETUP = "RUN_SETUP"    # 服务器→代理
    ACK = "ACK"                # 双向


class Envelope(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: MsgType
    ts: int = Field(default_factory=lambda: int(time.time()))
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- payloads
class Enroll(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    ip: str = ""              # 代理自报，仅供显示；服务器以连接的实际源地址为准
    token: str                # 注册令牌


class Register(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    token: str                # 代理专属 token
    hostname: str
    client_id: str | None = None
    ip: str = ""
    agent_version: str = ""
    manager_version: str | None = None
    manager_mode: Literal["service", "standalone", "missing"] = "missing"


class HubInfo(BaseModel):
    name: str
    address: str
    port: int = 18574
    serial: str | None = None


class Config(BaseModel):
    hubs: list[HubInfo]
    heartbeat_interval_seconds: float = 15
    client_id_pattern: str = "ukey-{hostname}"


class Heartbeat(BaseModel):
    connected_groups: list[str] = Field(default_factory=list)  # 本机已连接的分组地址（来自 Manager LIST）


class PortRef(BaseModel):
    """REQUEST / RELEASE：port_id 或 UKey label 二选一。"""
    port_id: str | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if bool(self.port_id) == bool(self.label):
            raise ValueError("port_id 与 label 必须且只能给一个")
        return self


class Event(BaseModel):
    kind: Literal["connected", "disconnected", "error"]
    port_id: str | None = None
    group: str | None = None
    detail: str = ""


class GroupCommand(BaseModel):
    """CONNECT / DISCONNECT。group = Manager 分组地址，如 AW08-017363.6。"""
    port_id: str
    group: str
    hub: str


class ReleaseAsk(BaseModel):
    port_id: str
    requester: str
    requester_host: str = ""


class Notify(BaseModel):
    text: str


class Ack(BaseModel):
    ref_id: str
    ok: bool
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)  # 例如 RELEASE_ASK 的 {"accept": true}、ENROLL 的 {"token": ...}


PAYLOADS: dict[MsgType, type[BaseModel] | None] = {
    MsgType.ENROLL: Enroll, MsgType.REGISTER: Register, MsgType.CONFIG: Config,
    MsgType.HEARTBEAT: Heartbeat, MsgType.REQUEST: PortRef, MsgType.RELEASE: PortRef,
    MsgType.EVENT: Event, MsgType.CONNECT: GroupCommand, MsgType.DISCONNECT: GroupCommand,
    MsgType.RELEASE_ASK: ReleaseAsk, MsgType.NOTIFY: Notify, MsgType.RUN_SETUP: None, MsgType.ACK: Ack,
}

# 服务器必须收到 ACK 的指令
NEEDS_ACK = {MsgType.CONNECT, MsgType.DISCONNECT, MsgType.RELEASE_ASK, MsgType.NOTIFY, MsgType.RUN_SETUP, MsgType.CONFIG}


class ProtocolError(ValueError):
    pass


def encode(env: Envelope) -> str:
    return env.model_dump_json()


def decode(raw: str | bytes) -> Envelope:
    """解析并校验一条消息（含 payload）；任何问题抛 ProtocolError。"""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message too large")
    try:
        env = Envelope.model_validate(json.loads(raw))
        model = PAYLOADS[env.type]
        if model is not None:
            model.model_validate(env.payload)
    except (ValueError, ValidationError, TypeError) as e:
        raise ProtocolError(f"invalid message: {e}") from e
    return env


def make(msg_type: MsgType, payload: BaseModel | dict | None = None, *, id: str | None = None) -> Envelope:
    data = payload.model_dump() if isinstance(payload, BaseModel) else (payload or {})
    kw = {"id": id} if id else {}
    return Envelope(type=msg_type, payload=data, **kw)


def ack(ref_id: str, ok: bool = True, error: str | None = None, **data: Any) -> Envelope:
    return make(MsgType.ACK, Ack(ref_id=ref_id, ok=ok, error=error, data=data))
