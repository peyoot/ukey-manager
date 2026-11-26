"""调度器：租约、分配、释放、超时回收、对账。

实测依据（阶段 0.5）：
- Hub 撤销授权会**立即强制断开**已连接的客户端 → 正常释放先让代理 DISCONNECT（优雅），再 revoke；
  代理失联 / 租约到期无响应时，服务器直接 revoke 兜底。
- 分组地址 = `<序列号>.<分组序号>`；端口 N = groupNN。
- 一个分组同一时刻只允许一个客户端连接。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Callable

from .db import Db
from .hub.base import HubAdapter, HubError, port_for_group
from .link import AgentLink


class Scheduler:
    def __init__(
        self,
        db: Db,
        hubs: dict[str, HubAdapter],
        link: AgentLink,
        *,
        lease_ttl: float = 600,
        heartbeat_timeout: float = 45,
        ack_timeout: float = 5,
        retries: int = 2,
        auto_revoke_orphans: bool = False,
        authorization_mode: str = "dynamic",
        clock: Callable[[], float] = time.time,
    ):
        self.db, self.hubs, self.link = db, hubs, link
        self.lease_ttl, self.heartbeat_timeout = lease_ttl, heartbeat_timeout
        self.ack_timeout, self.retries = ack_timeout, retries
        self.auto_revoke_orphans = auto_revoke_orphans
        self.authorization_mode = authorization_mode
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._hub_ok: dict[str, bool] = {}
        self._subscribers: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ 公共辅助
    def subscribe(self, cb: Callable[[dict], None]) -> None:
        """端口/代理状态变化回调（阶段 5 控制台 WebSocket 用）。"""
        self._subscribers.append(cb)

    def _emit(self, kind: str, **data) -> None:
        for cb in self._subscribers:
            try:
                cb({"event": kind, **data})
            except Exception:  # 订阅者出错不能影响调度
                pass

    def _audit(self, actor, agent_id, port_id, action, result, detail=None) -> None:
        self.db.audit(actor, agent_id, port_id, action, result, detail, ts=self.clock())

    def sync_ports(self, hub_cfgs, ukeys=()) -> list[str]:
        """按配置创建/更新端口记录。已移除的 Hub 只告警，不删除历史数据。返回告警列表。"""
        from .hub.base import group_name_for_port, port_id

        labels = {(u["hub"], u["port"]): u["label"] for u in ukeys}
        names = set()
        for h in hub_cfgs:
            names.add(h.name)
            for p in range(1, h.ports + 1):
                self.db.upsert_port(port_id(h.name, p), h.name, p, group_name_for_port(p), labels.get((h.name, p)))
        warnings = []
        for hub in {r["hub"] for r in self.db.list_ports()} - names:
            msg = f"Hub {hub} 已不在配置中；保留历史数据，未删除其端口记录"
            warnings.append(msg)
            self._audit("system", None, None, "hub_removed_warning", "warn", msg)
        return warnings

    def group_address(self, port: dict) -> str:
        hub = self.hubs[port["hub"]]
        n = port_for_group(port["grp"])
        return f"{getattr(hub, 'declared_serial', None) or port['hub']}.{n}"

    def view_port(self, port_id: str) -> dict:
        p = self.db.get_port(port_id)
        lease = self.db.active_lease(port_id)
        return {**p, "lease": lease}

    def _usable_agent(self, agent_id: str) -> tuple[dict | None, str | None]:
        a = self.db.get_agent(agent_id)
        if not a:
            return None, "unknown agent"
        if a["status"] != "approved":
            return None, "agent not approved"
        if not a["client_id"]:
            return None, "agent has no client_id"
        mode = json.loads(a.get("info") or "{}").get("manager_mode")
        if mode and mode != "service":
            return None, f"manager not ready (mode={mode})"
        if not self.link.is_online(agent_id) or not a["online"]:
            return None, "agent offline"
        return a, None

    # ------------------------------------------------------------------ 申请
    async def request(self, agent_id: str, port_id: str, actor: str = "agent") -> dict:
        port = self.db.get_port(port_id)
        if not port:
            return {"status": "error", "error": "unknown port"}
        async with self._locks[port_id]:
            agent, err = self._usable_agent(agent_id)
            if err:
                self._audit(actor, agent_id, port_id, "request", "denied", err)
                return {"status": "error", "error": err}
            port = self.db.get_port(port_id)
            if port["state"] == "fault":
                self._audit(actor, agent_id, port_id, "request", "denied", f"port fault: {port['fault_reason']}")
                return {"status": "error", "error": f"port fault: {port['fault_reason']}"}
            if port["state"] in ("leased", "connected"):
                if port["agent_id"] == agent_id:
                    self.db.extend_lease(port_id, self.clock() + self.lease_ttl)
                    self.db.set_port(port_id, lease_expires_at=self.clock() + self.lease_ttl)
                    return {"status": "ok", "already": True, "port": self.view_port(port_id)}
                return await self._contend(port, agent, actor)
            return await self._allocate(port, agent, actor)

    async def _contend(self, port: dict, agent: dict, actor: str) -> dict:
        holder = port["agent_id"]
        self._audit(actor, agent["agent_id"], port["port_id"], "request", "busy", f"holder={holder}")
        ack = await self.link.send(
            holder, "RELEASE_ASK",
            {"port_id": port["port_id"], "requester": agent["agent_id"], "requester_host": agent["hostname"]},
            timeout=self.ack_timeout, retries=0,
        )
        if ack.ok and ack.payload.get("accept") is True:
            self._audit("agent", holder, port["port_id"], "release_ask", "accepted")
            res = await self._release_locked(port, reason="yielded", graceful=True, actor=holder)
            if res["status"] != "ok":
                return {"status": "error", "error": f"yield failed: {res.get('error')}"}
            return await self._allocate(self.db.get_port(port["port_id"]), agent, actor)
        self._audit("agent", holder, port["port_id"], "release_ask", "declined", ack.error)
        return {"status": "busy", "holder": holder, "asked": True}

    async def _allocate(self, port: dict, agent: dict, actor: str) -> dict:
        pid, cid, aid = port["port_id"], agent["client_id"], agent["agent_id"]
        hub = self.hubs.get(port["hub"])
        if hub is None:
            return {"status": "error", "error": "hub not configured"}
        now = self.clock()
        exp = now + self.lease_ttl
        self.db.start_lease(pid, aid, cid, now, exp)
        self.db.set_port(pid, state="leased", client_id=cid, agent_id=aid, lease_expires_at=exp, fault_reason=None)
        try:
            await hub.add_client(cid)
            await hub.grant(cid, port["grp"])
        except HubError as e:
            self.db.end_lease(pid, self.clock(), "grant_failed")
            self.db.set_port(pid, state="idle", client_id=None, agent_id=None, lease_expires_at=None)
            self._audit(actor, aid, pid, "grant", "error", str(e))
            return {"status": "error", "error": f"hub: {e}"}
        self._audit(actor, aid, pid, "grant", "ok", f"{cid} -> {port['grp']}")
        ack = await self.link.send(
            aid, "CONNECT", {"port_id": pid, "group": self.group_address(port), "hub": port["hub"]},
            timeout=self.ack_timeout, retries=self.retries,
        )
        if not ack.ok:
            try:
                await hub.revoke(cid, port["grp"])
            except HubError as e:
                self.db.set_port(pid, state="fault", fault_reason=f"revoke after failed connect: {e}")
                self._audit("system", aid, pid, "revoke", "error", str(e))
                return {"status": "error", "error": f"connect failed ({ack.error}); revoke failed: {e}"}
            self.db.end_lease(pid, self.clock(), "connect_failed")
            self.db.set_port(pid, state="idle", client_id=None, agent_id=None, lease_expires_at=None)
            self._audit(actor, aid, pid, "connect", "error", ack.error)
            return {"status": "error", "error": f"connect failed: {ack.error}"}
        self.db.set_port(pid, state="connected")
        self._audit(actor, aid, pid, "connect", "ok")
        self._emit("port", port_id=pid)
        return {"status": "ok", "port": self.view_port(pid)}

    # ------------------------------------------------------------------ 释放
    async def release(self, agent_id: str, port_id: str, actor: str = "agent") -> dict:
        port = self.db.get_port(port_id)
        if not port:
            return {"status": "error", "error": "unknown port"}
        async with self._locks[port_id]:
            port = self.db.get_port(port_id)
            if port["state"] == "idle":
                return {"status": "ok", "already": True}
            if port["agent_id"] != agent_id:
                self._audit(actor, agent_id, port_id, "release", "denied", f"holder={port['agent_id']}")
                return {"status": "error", "error": "not the holder"}
            return await self._release_locked(port, reason="released", graceful=True, actor=actor)

    async def force_release(self, port_id: str, actor: str = "admin") -> dict:
        port = self.db.get_port(port_id)
        if not port:
            return {"status": "error", "error": "unknown port"}
        async with self._locks[port_id]:
            port = self.db.get_port(port_id)
            if port["state"] == "idle":
                return {"status": "ok", "already": True}
            return await self._release_locked(port, reason="forced", graceful=True, actor=actor)

    async def _release_locked(self, port: dict, *, reason: str, graceful: bool, actor: str) -> dict:
        """调用方须持有该端口的锁。graceful：先让代理 DISCONNECT（失败也继续），然后一定 revoke。"""
        pid, cid, aid = port["port_id"], port["client_id"], port["agent_id"]
        detail = reason
        if graceful and aid and self.link.is_online(aid):
            ack = await self.link.send(
                aid, "DISCONNECT", {"port_id": pid, "group": self.group_address(port), "hub": port["hub"]},
                timeout=self.ack_timeout, retries=self.retries,
            )
            if not ack.ok:
                detail += f"; DISCONNECT 无效: {ack.error}（继续撤销授权）"
        hub = self.hubs.get(port["hub"])
        try:
            if hub is None:
                raise HubError("hub not configured")
            if cid:
                await hub.revoke(cid, port["grp"])
        except HubError as e:
            self.db.set_port(pid, state="fault", fault_reason=f"revoke failed: {e}")
            self._audit(actor, aid, pid, "revoke", "error", f"{detail}; {e}")
            self._emit("port", port_id=pid)
            return {"status": "error", "error": f"revoke failed: {e}"}
        self.db.end_lease(pid, self.clock(), reason)
        self.db.set_port(pid, state="idle", client_id=None, agent_id=None, lease_expires_at=None, fault_reason=None)
        self._audit(actor, aid, pid, "release", "ok", detail)
        self._emit("port", port_id=pid)
        return {"status": "ok", "reason": reason}

    async def renew(self, agent_id: str, port_id: str) -> dict:
        async with self._locks[port_id]:
            port = self.db.get_port(port_id)
            if not port or port["agent_id"] != agent_id or port["state"] not in ("leased", "connected"):
                return {"status": "error", "error": "no such lease"}
            exp = self.clock() + self.lease_ttl
            self.db.extend_lease(port_id, exp)
            self.db.set_port(port_id, lease_expires_at=exp)
            return {"status": "ok", "expires_at": exp}

    # ------------------------------------------------------------------ 心跳与兜底
    async def on_heartbeat(self, agent_id: str, connected_groups: list[str]) -> None:
        """代理心跳：刷新存活；若持租约的代理没有报告对应分组已连接，说明本机已断开 → 结束租约。"""
        a = self.db.get_agent(agent_id)
        if not a:
            return
        self.db.set_agent(agent_id, online=1, last_heartbeat=self.clock())
        for port in self.db.list_ports():
            if port["agent_id"] != agent_id or port["state"] != "connected":
                continue
            lease = self.db.active_lease(port["port_id"])
            if lease and self.clock() - lease["started_at"] < 2 * self.ack_timeout:
                continue  # 刚连接，给代理一点时间
            if self.group_address(port) not in connected_groups:
                async with self._locks[port["port_id"]]:
                    cur = self.db.get_port(port["port_id"])
                    if cur["state"] == "connected" and cur["agent_id"] == agent_id:
                        await self._release_locked(cur, reason="agent_disconnected", graceful=False, actor="system")

    async def sweep(self) -> None:
        """周期任务：心跳超时 → 直接 revoke（不等代理）；租约到期 → 尝试优雅断开后 revoke。"""
        now = self.clock()
        for a in self.db.list_agents():
            if a["online"] and now - (a["last_heartbeat"] or 0) > self.heartbeat_timeout:
                self.db.set_agent(a["agent_id"], online=0)
                self._audit("system", a["agent_id"], None, "agent_timeout", "warn", "heartbeat timeout")
                self._emit("agent", agent_id=a["agent_id"])
        for port in self.db.list_ports():
            if port["state"] not in ("leased", "connected"):
                continue
            agent = self.db.get_agent(port["agent_id"]) if port["agent_id"] else None
            lost = agent is None or not agent["online"]
            expired = port["lease_expires_at"] is not None and port["lease_expires_at"] <= now
            if not (lost or expired):
                continue
            async with self._locks[port["port_id"]]:
                cur = self.db.get_port(port["port_id"])
                if cur["state"] not in ("leased", "connected") or cur["agent_id"] != port["agent_id"]:
                    continue
                await self._release_locked(
                    cur, reason="agent_lost" if lost else "expired", graceful=not lost, actor="system"
                )

    # ------------------------------------------------------------------ Hub 健康与对账
    async def check_hubs(self) -> None:
        """Hub 离线只把它自己的端口标 fault；恢复后对账并还原。"""
        for name, hub in self.hubs.items():
            h = await hub.health()
            was_ok = self._hub_ok.get(name, True)
            self._hub_ok[name] = h.ok
            if not h.ok and was_ok:
                for p in self.db.list_ports(name):
                    self.db.set_port(p["port_id"], state="fault", fault_reason=f"hub offline: {h.error}")
                self._audit("system", None, None, "hub_offline", "error", f"{name}: {h.error}")
                self._emit("hub", hub=name)
            elif h.ok and not was_ok:
                self._audit("system", None, None, "hub_online", "ok", name)
                await self.recover_hub(name)
                self._emit("hub", hub=name)

    async def recover_hub(self, name: str) -> None:
        hub = self.hubs[name]
        try:
            clients = {c.id: c.groups for c in await hub.list_clients()}
        except HubError as e:
            self._hub_ok[name] = False
            self._audit("system", None, None, "hub_recover", "error", str(e))
            return
        for p in self.db.list_ports(name):
            if p["state"] != "fault":
                continue
            lease = self.db.active_lease(p["port_id"])
            granted = bool(lease and p["grp"] in clients.get(lease["client_id"], []))
            async with self._locks[p["port_id"]]:
                if granted:
                    self.db.set_port(p["port_id"], state="connected", fault_reason=None)
                else:
                    self.db.end_lease(p["port_id"], self.clock(), "hub_recovered_no_grant")
                    self.db.set_port(p["port_id"], state="idle", client_id=None, agent_id=None,
                                     lease_expires_at=None, fault_reason=None)
        await self.reconcile(name)

    async def reconcile(self, hub_name: str | None = None) -> list[str]:
        """对账：Hub 上存在、数据库无租约的授权 = 孤儿，告警（auto_revoke_orphan_grants 时自动回收）。
        数据库有租约、Hub 上却没有授权 = 结束租约。静态授权模式不查孤儿。"""
        findings: list[str] = []
        for name in [hub_name] if hub_name else list(self.hubs):
            hub = self.hubs[name]
            try:
                clients = await hub.list_clients()
            except HubError as e:
                findings.append(f"{name}: 读取失败 {e}")
                self._audit("system", None, None, "reconcile", "error", f"{name}: {e}")
                continue
            by_group = {p["grp"]: p for p in self.db.list_ports(name)}
            if self.authorization_mode != "static":
                for c in clients:
                    for g in c.groups:
                        p = by_group.get(g)
                        lease = self.db.active_lease(p["port_id"]) if p else None
                        if p and (lease is None or lease["client_id"] != c.id) or p is None:
                            msg = f"{name}: 孤儿授权 {c.id} -> {g}（数据库无对应租约）"
                            findings.append(msg)
                            self._audit("system", None, p["port_id"] if p else None, "orphan_grant", "warn", msg)
                            if self.auto_revoke_orphans and p:
                                try:
                                    await hub.revoke(c.id, g)
                                    self._audit("system", None, p["port_id"], "orphan_revoke", "ok", msg)
                                except HubError as e:
                                    self._audit("system", None, p["port_id"], "orphan_revoke", "error", str(e))
            granted = {(c.id, g) for c in clients for g in c.groups}
            for p in by_group.values():
                lease = self.db.active_lease(p["port_id"])
                if lease and p["state"] in ("leased", "connected") and (lease["client_id"], p["grp"]) not in granted:
                    async with self._locks[p["port_id"]]:
                        self.db.end_lease(p["port_id"], self.clock(), "grant_missing")
                        self.db.set_port(p["port_id"], state="idle", client_id=None, agent_id=None,
                                         lease_expires_at=None)
                    msg = f"{p['port_id']}: 租约存在但 Hub 上无授权，已结束租约"
                    findings.append(msg)
                    self._audit("system", lease["agent_id"], p["port_id"], "grant_missing", "warn", msg)
        return findings

    # ------------------------------------------------------------------ 代理登记（阶段 3 的 ENROLL/REGISTER 调用）
    def register_agent(self, agent_id: str, hostname: str, ip: str, client_id: str | None,
                       auto_approve_ips: list[str] = (), info: dict | None = None) -> dict:
        existing = self.db.get_agent(agent_id)
        status = existing["status"] if existing else ("approved" if ip in auto_approve_ips else "pending")
        self.db.upsert_agent(agent_id, hostname, ip, client_id, status, info)
        self.db.set_agent(agent_id, online=1, last_heartbeat=self.clock())
        self._audit("agent", agent_id, None, "register", "ok", f"status={status} ip={ip}")
        self._emit("agent", agent_id=agent_id)
        return self.db.get_agent(agent_id)

    def approve_agent(self, agent_id: str, actor: str = "admin") -> bool:
        if not self.db.get_agent(agent_id):
            return False
        self.db.set_agent(agent_id, status="approved")
        self._audit(actor, agent_id, None, "approve_agent", "ok")
        self._emit("agent", agent_id=agent_id)
        return True
