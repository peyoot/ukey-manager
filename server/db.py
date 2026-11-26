"""SQLite：ports / leases / agents / audit_log。同步 sqlite3 + 锁（演示规模足够，调用都很短）。

端口状态 state：idle | leased | connected | fault
租约结束原因 end_reason：released | forced | expired | agent_lost | yielded | grant_failed |
                         connect_failed | agent_disconnected | grant_missing | hub_recovered_no_grant
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
  port_id TEXT PRIMARY KEY, hub TEXT NOT NULL, port INTEGER NOT NULL, grp TEXT NOT NULL,
  label TEXT, state TEXT NOT NULL DEFAULT 'idle', client_id TEXT, agent_id TEXT,
  lease_expires_at REAL, fault_reason TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY, hostname TEXT, ip TEXT, client_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',          -- pending | approved | rejected
  online INTEGER NOT NULL DEFAULT 0, last_heartbeat REAL, info TEXT, token_hash TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, port_id TEXT NOT NULL, agent_id TEXT NOT NULL, client_id TEXT NOT NULL,
  started_at REAL NOT NULL, expires_at REAL NOT NULL, ended_at REAL, end_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_active ON leases(port_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT, agent_id TEXT, port_id TEXT,
  action TEXT NOT NULL, result TEXT NOT NULL, detail TEXT
);
"""


class Db:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._c.execute(sql, args).lastrowid

    # ---------------------------------------------------------------- ports
    def upsert_port(self, port_id: str, hub: str, port: int, grp: str, label: str | None) -> None:
        self.x(
            "INSERT INTO ports(port_id,hub,port,grp,label,state,updated_at) VALUES(?,?,?,?,?,'idle',?) "
            "ON CONFLICT(port_id) DO UPDATE SET hub=excluded.hub, port=excluded.port, grp=excluded.grp, "
            "label=COALESCE(excluded.label, ports.label)",
            (port_id, hub, port, grp, label, time.time()),
        )

    def get_port(self, port_id: str) -> dict | None:
        return self.one("SELECT * FROM ports WHERE port_id=?", (port_id,))

    def list_ports(self, hub: str | None = None) -> list[dict]:
        if hub:
            return self.q("SELECT * FROM ports WHERE hub=? ORDER BY port", (hub,))
        return self.q("SELECT * FROM ports ORDER BY hub, port")

    def set_port(self, port_id: str, **fields: Any) -> None:
        allowed = {"state", "client_id", "agent_id", "lease_expires_at", "fault_reason", "label"}
        assert set(fields) <= allowed, set(fields) - allowed
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE ports SET {sets} WHERE port_id=?", (*fields.values(), port_id))

    # ---------------------------------------------------------------- agents
    def upsert_agent(self, agent_id: str, hostname: str, ip: str, client_id: str | None,
                     status: str = "pending", info: dict | None = None) -> None:
        self.x(
            "INSERT INTO agents(agent_id,hostname,ip,client_id,status,info,created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET hostname=excluded.hostname, ip=excluded.ip, "
            "client_id=COALESCE(excluded.client_id, agents.client_id), info=excluded.info",
            (agent_id, hostname, ip, client_id, status, json.dumps(info or {}), time.time()),
        )

    def get_agent(self, agent_id: str) -> dict | None:
        return self.one("SELECT * FROM agents WHERE agent_id=?", (agent_id,))

    def list_agents(self) -> list[dict]:
        return self.q("SELECT * FROM agents ORDER BY agent_id")

    def set_agent(self, agent_id: str, **fields: Any) -> None:
        allowed = {"status", "online", "last_heartbeat", "client_id", "info", "token_hash", "ip", "hostname"}
        assert set(fields) <= allowed, set(fields) - allowed
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE agents SET {sets} WHERE agent_id=?", (*fields.values(), agent_id))

    # ---------------------------------------------------------------- leases
    def start_lease(self, port_id: str, agent_id: str, client_id: str, now: float, expires_at: float) -> int:
        return self.x(
            "INSERT INTO leases(port_id,agent_id,client_id,started_at,expires_at) VALUES(?,?,?,?,?)",
            (port_id, agent_id, client_id, now, expires_at),
        )

    def active_lease(self, port_id: str) -> dict | None:
        return self.one("SELECT * FROM leases WHERE port_id=? AND ended_at IS NULL", (port_id,))

    def end_lease(self, port_id: str, now: float, reason: str) -> None:
        self.x("UPDATE leases SET ended_at=?, end_reason=? WHERE port_id=? AND ended_at IS NULL", (now, reason, port_id))

    def extend_lease(self, port_id: str, expires_at: float) -> None:
        self.x("UPDATE leases SET expires_at=? WHERE port_id=? AND ended_at IS NULL", (expires_at, port_id))

    def list_leases(self, active_only: bool = False) -> list[dict]:
        where = "WHERE ended_at IS NULL" if active_only else ""
        return self.q(f"SELECT * FROM leases {where} ORDER BY id")

    # ---------------------------------------------------------------- audit
    def audit(self, actor: str | None, agent_id: str | None, port_id: str | None, action: str,
              result: str, detail: str | None = None, ts: float | None = None) -> None:
        self.x(
            "INSERT INTO audit_log(ts,actor,agent_id,port_id,action,result,detail) VALUES(?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), actor, agent_id, port_id, action, result, detail),
        )

    def list_audit(self, limit: int = 200, port_id: str | None = None) -> list[dict]:
        if port_id:
            return self.q("SELECT * FROM audit_log WHERE port_id=? ORDER BY id DESC LIMIT ?", (port_id, limit))
        return self.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
