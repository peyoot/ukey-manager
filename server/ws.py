"""代理 WebSocket 通道（wss://<server>/ws/agent）：AgentHub 同时实现调度器用的 AgentLink。

要点：
- 认证：ENROLL（注册令牌）→ 管理员批准 → ACK 下发代理专属 token（只发一次，库里只存 SHA-256）；
  之后每次连接先 REGISTER（专属 token）。
- 服务器指令必须 ACK：同一 id 重发即重试，超时与重试次数由调度器传入；代理按 id 去重。
- 代理消息按 id 去重：重复的 id 直接重发上次回复，不重复执行。
- 收消息循环只做分发：REQUEST/RELEASE 等耗时处理放到后台任务，否则服务器在处理 REQUEST 时下发的
  CONNECT 的 ACK 无人读取，会死锁。
- 代理的 IP 取自连接的源地址（自报的 ip 只作显示）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections import OrderedDict

from fastapi import WebSocket, WebSocketDisconnect

from common import protocol as P
from common.protocol import MsgType

from .db import Db
from .link import Ack

REPLY_CACHE = 500


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Conn:
    def __init__(self, ws: WebSocket, agent_id: str):
        self.ws, self.agent_id = ws, agent_id
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.replies: OrderedDict[str, P.Envelope] = OrderedDict()
        self.inflight: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    async def send(self, env: P.Envelope) -> None:
        async with self.send_lock:
            await self.ws.send_text(P.encode(env))


class AgentHub:
    def __init__(
        self,
        db: Db,
        *,
        enrollment_token: str,
        auto_approve_ips: list[str] = (),
        hubs_info: list[dict] = (),
        heartbeat_interval: float = 15,
        client_id_pattern: str = "ukey-{hostname}",
        idle_timeout: float = 120,
    ):
        self.db = db
        self.enrollment_token = enrollment_token
        self.auto_approve_ips = list(auto_approve_ips)
        self.hubs_info = list(hubs_info)
        self.heartbeat_interval = heartbeat_interval
        self.client_id_pattern = client_id_pattern
        self.idle_timeout = idle_timeout
        self.conns: dict[str, _Conn] = {}
        self.sched = None

    def bind(self, sched) -> None:
        self.sched = sched

    # ------------------------------------------------------------------ AgentLink
    def is_online(self, agent_id: str) -> bool:
        return agent_id in self.conns

    async def send(self, agent_id: str, msg_type: str, payload: dict, *, timeout: float, retries: int) -> Ack:
        conn = self.conns.get(agent_id)
        if conn is None:
            return Ack(False, "agent offline")
        env = P.make(MsgType(msg_type), payload)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.pending[env.id] = fut
        try:
            for _ in range(retries + 1):
                try:
                    await conn.send(env)  # 重试时 id 不变，代理据此去重
                except Exception as e:
                    return Ack(False, f"send failed: {type(e).__name__}")
                try:
                    a: P.Ack = await asyncio.wait_for(asyncio.shield(fut), timeout)
                    return Ack(a.ok, a.error, a.data)
                except asyncio.TimeoutError:
                    continue
            return Ack(False, "timeout")
        finally:
            conn.pending.pop(env.id, None)

    async def run_setup(self, agent_id: str) -> Ack:
        return await self.send(agent_id, "RUN_SETUP", {}, timeout=5, retries=1)

    async def notify(self, agent_id: str, text: str) -> Ack:
        return await self.send(agent_id, "NOTIFY", {"text": text}, timeout=5, retries=1)

    # ------------------------------------------------------------------ 连接处理
    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        ip = ws.client.host if ws.client else ""
        conn: _Conn | None = None
        try:
            first = await self._recv(ws, 30)
            if first is None:
                return
            if first.type == MsgType.ENROLL:
                await self._enroll(ws, first, ip)
                return
            if first.type != MsgType.REGISTER:
                await self._reply(ws, P.ack(first.id, False, "expected REGISTER or ENROLL"))
                await ws.close(4400)
                return
            conn = await self._register(ws, first, ip)
            if conn is None:
                return
            await self._loop(conn)
        except WebSocketDisconnect:
            pass
        finally:
            if conn is not None:
                await self._cleanup(conn)

    @staticmethod
    async def _reply(ws: WebSocket, env: P.Envelope) -> None:
        try:
            await ws.send_text(P.encode(env))
        except Exception:
            pass

    async def _recv(self, ws: WebSocket, timeout: float) -> P.Envelope | None:
        """收一条已校验的消息；超时/格式错返回 None（调用方关闭连接）。"""
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout)
        except asyncio.TimeoutError:
            await ws.close(4408)
            return None
        try:
            return P.decode(raw)
        except P.ProtocolError as e:
            await self._reply(ws, P.ack(self._raw_id(raw), False, str(e)[:200]))
            await ws.close(4400)
            return None

    @staticmethod
    def _raw_id(raw: str) -> str:
        try:
            return str(json.loads(raw).get("id", ""))
        except Exception:
            return ""

    async def _enroll(self, ws: WebSocket, env: P.Envelope, ip: str) -> None:
        req = P.Enroll.model_validate(env.payload)
        if not hmac.compare_digest(req.token.encode(), self.enrollment_token.encode()):
            self.db.audit("agent", None, None, "enroll", "denied", f"bad enrollment token from {ip}")
            await self._reply(ws, P.ack(env.id, False, "bad enrollment token"))
            await ws.close(4401)
            return
        agent_id = req.hostname.strip().lower()
        existing = self.db.get_agent(agent_id)
        status = existing["status"] if existing else ("approved" if ip in self.auto_approve_ips else "pending")
        self.db.upsert_agent(agent_id, req.hostname, ip, None, status, {"reported_ip": req.ip})
        if status == "rejected":
            reply = P.ack(env.id, False, "rejected by administrator")
        elif status == "pending":
            self.db.audit("agent", agent_id, None, "enroll", "pending", f"ip={ip}")
            reply = P.ack(env.id, True, agent_id=agent_id, status="pending")
        elif existing and existing["token_hash"]:
            reply = P.ack(env.id, False, "already enrolled; ask the administrator to reset the token")
        else:
            token = secrets.token_urlsafe(32)
            self.db.set_agent(agent_id, token_hash=_hash(token))
            self.db.audit("agent", agent_id, None, "enroll", "ok", f"approved, token issued; ip={ip}")
            reply = P.ack(env.id, True, agent_id=agent_id, status="approved", token=token)
        await self._reply(ws, reply)
        await ws.close(1000)

    async def _register(self, ws: WebSocket, env: P.Envelope, ip: str) -> _Conn | None:
        req = P.Register.model_validate(env.payload)
        agent = self.db.get_agent(req.agent_id)
        ok = bool(
            agent and agent["token_hash"] and agent["status"] == "approved"
            and hmac.compare_digest(_hash(req.token), agent["token_hash"])
        )
        if not ok:
            self.db.audit("agent", req.agent_id[:64], None, "register", "denied", f"from {ip}")
            await self._reply(ws, P.ack(env.id, False, "unauthorized"))
            await ws.close(4401)
            return None
        old = self.conns.get(req.agent_id)
        conn = _Conn(ws, req.agent_id)
        self.conns[req.agent_id] = conn
        if old is not None:
            try:
                await old.ws.close(4000)
            except Exception:
                pass
        info = {
            "manager_mode": req.manager_mode, "manager_version": req.manager_version,
            "agent_version": req.agent_version, "reported_ip": req.ip,
        }
        self.sched.register_agent(req.agent_id, req.hostname, ip, req.client_id, self.auto_approve_ips, info)
        await conn.send(P.ack(env.id, True, agent_id=req.agent_id))
        self._spawn(conn, self._push_config(conn))
        return conn

    async def _push_config(self, conn: _Conn) -> None:
        cfg = P.Config(
            hubs=[P.HubInfo(**h) for h in self.hubs_info],
            heartbeat_interval_seconds=self.heartbeat_interval,
            client_id_pattern=self.client_id_pattern,
        )
        a = await self.send(conn.agent_id, "CONFIG", cfg.model_dump(), timeout=5, retries=2)
        if not a.ok:
            self.db.audit("system", conn.agent_id, None, "config", "error", a.error)

    def _spawn(self, conn: _Conn, coro) -> None:
        t = asyncio.create_task(coro)
        conn.tasks.add(t)
        t.add_done_callback(conn.tasks.discard)

    async def _loop(self, conn: _Conn) -> None:
        while True:
            env = await self._recv(conn.ws, self.idle_timeout)
            if env is None:
                return
            if env.type == MsgType.ACK:
                a = P.Ack.model_validate(env.payload)
                fut = conn.pending.get(a.ref_id)
                if fut and not fut.done():
                    fut.set_result(a)
                continue
            if env.id in conn.replies:  # 重复（对方重试）：重发上次回复，不重复执行
                await conn.send(conn.replies[env.id])
                continue
            if env.id in conn.inflight:
                continue
            conn.inflight.add(env.id)
            self._spawn(conn, self._process(conn, env))

    async def _process(self, conn: _Conn, env: P.Envelope) -> None:
        try:
            reply = await self._dispatch(conn, env)
        except Exception as e:  # 单条消息处理失败不能拖垮连接
            self.db.audit("system", conn.agent_id, None, "ws_error", "error", f"{env.type.value}: {type(e).__name__}: {e}")
            reply = P.ack(env.id, False, "internal error")
        conn.inflight.discard(env.id)
        conn.replies[env.id] = reply
        while len(conn.replies) > REPLY_CACHE:
            conn.replies.popitem(last=False)
        try:
            await conn.send(reply)
        except Exception:
            pass

    def _resolve_port(self, ref: P.PortRef) -> str | None:
        if ref.port_id:
            return ref.port_id
        rows = [p for p in self.db.list_ports() if p["label"] == ref.label]
        return rows[0]["port_id"] if len(rows) == 1 else None

    async def _dispatch(self, conn: _Conn, env: P.Envelope) -> P.Envelope:
        aid, t = conn.agent_id, env.type
        if t == MsgType.HEARTBEAT:
            await self.sched.on_heartbeat(aid, P.Heartbeat.model_validate(env.payload).connected_groups)
            return P.ack(env.id)
        if t in (MsgType.REQUEST, MsgType.RELEASE):
            ref = P.PortRef.model_validate(env.payload)
            port_id = self._resolve_port(ref)
            if not port_id:
                return P.ack(env.id, False, "unknown or ambiguous port/label")
            fn = self.sched.request if t == MsgType.REQUEST else self.sched.release
            res = await fn(aid, port_id)
            ok = res["status"] == "ok"
            err = None if ok else (res.get("error") or res["status"])
            return P.ack(env.id, ok, err, status=res["status"], port_id=port_id, holder=res.get("holder"))
        if t == MsgType.LIST:
            ports = [
                {"port_id": p["port_id"], "hub": p["hub"], "port": p["port"], "label": p["label"], "state": p["state"],
                 "holder": p["agent_id"], "mine": p["agent_id"] == aid}
                for p in self.db.list_ports()
            ]
            return P.ack(env.id, ports=ports)
        if t == MsgType.EVENT:
            ev = P.Event.model_validate(env.payload)
            self.db.audit("agent", aid, ev.port_id, f"event_{ev.kind}", "ok" if ev.kind != "error" else "error",
                          f"{ev.group or ''} {ev.detail}".strip())
            return P.ack(env.id)
        return P.ack(env.id, False, f"unexpected {t.value} from agent")

    async def _cleanup(self, conn: _Conn) -> None:
        if self.conns.get(conn.agent_id) is conn:
            del self.conns[conn.agent_id]
            self.db.audit("system", conn.agent_id, None, "agent_disconnected", "warn", "websocket closed")
        for mid, fut in list(conn.pending.items()):
            if not fut.done():
                fut.set_result(P.Ack(ref_id=mid, ok=False, error="disconnected"))
        for t in list(conn.tasks):
            t.cancel()
