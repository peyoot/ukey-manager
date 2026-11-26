"""代理 ↔ 服务器端到端：真实 TCP/WebSocket，代理用 MockManager（Linux 上无 awusbmanager.exe）。"""
import asyncio
import json
import os
import stat

import httpx
import pytest
import uvicorn
import yaml

from agent.agent import Agent, AgentConfig
from agent.manager_mock import MockManager, MockSystem
from server.app import create_app
from server.config import AppConfig
from server.db import Db
from server.ws import AgentHub
from tests.conftest import make_hub

CFG = {
    "hubs": [{"name": "hub-a", "address": "10.70.1.208", "ports": 8, "password_env": "X", "serial": "AW08-A"}],
    "lease": {"ttl_seconds": 600, "command_ack_timeout_seconds": 0.5, "command_retries": 1,
              "heartbeat_interval_seconds": 1, "heartbeat_timeout_seconds": 2},
    "ukeys": [{"hub": "hub-a", "port": 6, "label": "demo disk"}],
}


async def wait_for(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


class Stack:
    pass


@pytest.fixture
async def stack(tmp_path):
    s = Stack()
    s.db = Db(":memory:")
    s.hub = await make_hub("hub-a", 8, "AW08-A", tmp_path)
    s.link = AgentHub(
        s.db, enrollment_token="tok", auto_approve_ips=["127.0.0.1"], heartbeat_interval=1,
        hubs_info=[{"name": "hub-a", "address": "10.70.1.208", "port": 18574, "serial": "AW08-A"}],
    )
    s.app = create_app(AppConfig.model_validate(CFG), {"hub-a": s.hub}, s.link, s.db, admin_user="admin",
                       admin_password="pw", session_secret="k" * 32, cookie_secure=False, background=False)
    s.sched = s.app.state.scheduler
    s.server = uvicorn.Server(uvicorn.Config(s.app, host="127.0.0.1", port=0, log_level="warning"))
    s.srv_task = asyncio.create_task(s.server.serve())
    for _ in range(100):
        if s.server.started:
            break
        await asyncio.sleep(0.05)
    s.addr = f"127.0.0.1:{s.server.servers[0].sockets[0].getsockname()[1]}"
    s.agents: list[tuple[Agent, asyncio.Task]] = []
    s.tmp = tmp_path

    async def start_agent(name, *, policy="accept", token="tok", manager=None, ui=False, **kw):
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"server_url": f"ws://{s.addr}/ws/agent", "enrollment_token": token,
                                        "release_policy": policy, "hostname": name}))
        cfg = AgentConfig.load(path)
        mgr = manager or MockManager(client_id=f"ukey-{name.lower()}")
        ag = Agent(cfg, mgr, MockSystem("service"), base_backoff=0.05, max_backoff=0.2, enroll_poll=0.1, **kw)
        ag.mgr = mgr
        ag.ui_server = await ag.start_ui(0) if ui else None
        t = asyncio.create_task(ag.run())
        s.agents.append((ag, t))
        return ag

    s.start_agent = start_agent
    yield s
    for ag, t in s.agents:
        ag.stop()
        t.cancel()
        if ag.ui_server:
            ag.ui_server.close()
    s.server.should_exit = True
    await asyncio.wait_for(s.srv_task, 5)


async def test_enroll_register_config_and_heartbeat(stack):
    ag = await stack.start_agent("VM-Win-1")
    assert await wait_for(lambda: ag.registered.is_set())
    saved = yaml.safe_load(open(ag.cfg.path))
    assert saved["agent_token"] and saved["enrollment_token"] == ""   # token 换成专属 token，注册令牌清空
    assert stat.S_IMODE(os.stat(ag.cfg.path).st_mode) == 0o600
    assert await wait_for(lambda: ag.mgr.known_hubs == ["10.70.1.208"] and ag.mgr.autofind is False)  # CONFIG 已应用
    row = stack.db.get_agent("vm-win-1")
    assert row["status"] == "approved" and row["online"] == 1 and row["client_id"] == "ukey-vm-win-1"
    hb1 = row["last_heartbeat"]
    assert await wait_for(lambda: stack.db.get_agent("vm-win-1")["last_heartbeat"] > hb1)  # 心跳在走


async def test_request_connects_via_manager_and_release_disconnects(stack):
    ag = await stack.start_agent("VM-Win-1")
    assert await wait_for(lambda: ag.registered.is_set())
    ack = await ag.call(__import__("common.protocol", fromlist=["x"]).MsgType.REQUEST,
                        __import__("common.protocol", fromlist=["x"]).PortRef(label="demo disk"))
    assert ack.ok and ack.data["port_id"] == "hub-a-p06"
    assert ag.mgr.connected == {"AW08-A.6"}
    assert [c.groups for c in await stack.hub.list_clients()] == [["group06"]]
    assert await wait_for(lambda: any(a["action"] == "event_connected" for a in stack.db.list_audit()))
    res = await ag._ui_command({"cmd": "release", "port_id": "hub-a-p06"})
    assert res["ok"] and ag.mgr.connected == set()
    assert stack.db.get_port("hub-a-p06")["state"] == "idle"
    assert [c.groups for c in await stack.hub.list_clients()] == [[]]


async def test_pending_agent_waits_for_approval_then_connects(stack):
    stack.link.auto_approve_ips = []   # 不再自动批准
    ag = await stack.start_agent("VM-Pending")
    assert await wait_for(lambda: stack.db.get_agent("vm-pending") is not None)
    await asyncio.sleep(0.3)
    assert not ag.registered.is_set() and ag.cfg.agent_token == ""
    stack.sched.approve_agent("vm-pending")
    assert await wait_for(lambda: ag.registered.is_set())


async def test_wrong_enrollment_token_never_registers(stack):
    ag = await stack.start_agent("VM-Bad", token="wrong")
    await asyncio.sleep(0.4)
    assert not ag.registered.is_set() and stack.db.get_agent("vm-bad") is None


async def test_two_agents_contention_yield_and_deny(stack):
    a1 = await stack.start_agent("VM-Win-1")
    a2 = await stack.start_agent("VM-Win-2")
    assert await wait_for(lambda: a1.registered.is_set() and a2.registered.is_set())
    assert (await a1._ui_command({"cmd": "request", "port_id": "hub-a-p06"}))["ok"]
    r = await a2._ui_command({"cmd": "request", "port_id": "hub-a-p06"})       # a1 同意让出
    assert r["ok"] and a2.mgr.connected == {"AW08-A.6"} and a1.mgr.connected == set()
    assert any("同意" in n for n in a1.notices)
    assert stack.db.get_port("hub-a-p06")["agent_id"] == "vm-win-2"
    a2.cfg.release_policy = "deny"                                              # 现在 a2 拒绝
    r = await a1._ui_command({"cmd": "request", "port_id": "hub-a-p06"})
    assert not r["ok"] and r["data"]["status"] == "busy" and a2.mgr.connected == {"AW08-A.6"}


async def test_manager_connect_failure_rolls_back_on_server(stack):
    mgr = MockManager(client_id="ukey-vm-win-1", visible=set())   # 分组不可见 → CONNECT 失败
    ag = await stack.start_agent("VM-Win-1", manager=mgr)
    assert await wait_for(lambda: ag.registered.is_set())
    r = await ag._ui_command({"cmd": "request", "port_id": "hub-a-p06"})
    assert not r["ok"] and "connect failed" in r["error"]
    assert stack.db.get_port("hub-a-p06")["state"] == "idle"
    assert [c.groups for c in await stack.hub.list_clients()] == [[]]


async def test_reconnects_after_server_drops_connection_and_keeps_working(stack):
    ag = await stack.start_agent("VM-Win-1")
    assert await wait_for(lambda: ag.registered.is_set())
    first_ws = ag.ws
    await stack.link.conns["vm-win-1"].ws.close(1011)   # 服务器端踢掉
    assert await wait_for(lambda: ag.registered.is_set() and ag.ws is not first_ws)
    assert (await ag._ui_command({"cmd": "request", "port_id": "hub-a-p03"}))["ok"]


async def test_agent_crash_then_server_revokes_after_heartbeat_timeout(stack):
    ag = await stack.start_agent("VM-Win-1")
    assert await wait_for(lambda: ag.registered.is_set())
    assert (await ag._ui_command({"cmd": "request", "port_id": "hub-a-p06"}))["ok"]
    ag.stop()
    t = [t for a, t in stack.agents if a is ag][0]
    t.cancel()                                           # 代理进程"崩溃"
    await asyncio.sleep(0)
    assert await wait_for(lambda: "vm-win-1" not in stack.link.conns)
    await asyncio.sleep(2.2)                             # 超过 heartbeat_timeout_seconds=2
    await stack.sched.sweep()
    assert stack.db.get_port("hub-a-p06")["state"] == "idle"
    assert [c.groups for c in await stack.hub.list_clients()] == [[]]
    assert stack.db.list_leases()[0]["end_reason"] == "agent_lost"


async def test_local_ui_over_loopback(stack):
    ag = await stack.start_agent("VM-Win-1", ui=True)
    assert await wait_for(lambda: ag.registered.is_set())
    port = ag.ui_server.sockets[0].getsockname()[1]
    assert ag.ui_server.sockets[0].getsockname()[0] == "127.0.0.1"   # 只绑回环

    async def ask(req):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write((json.dumps(req) + "\n").encode())
        await w.drain()
        out = json.loads(await r.readline())
        w.close()
        return out

    st = await ask({"cmd": "status"})
    assert st["connected"] and st["client_id"] == "ukey-vm-win-1" and st["manager_mode"] == "service"
    ports = await ask({"cmd": "ports"})
    assert ports["ok"] and len(ports["ports"]) == 8
    assert next(p for p in ports["ports"] if p["port_id"] == "hub-a-p06")["label"] == "demo disk"
    assert (await ask({"cmd": "request", "label": "demo disk"}))["ok"]
    assert (await ask({"cmd": "ports"}))["ports"][5]["mine"] is True
    assert (await ask({"cmd": "bogus"}))["ok"] is False


async def test_run_setup_from_server_runs_setup_manager(stack):
    ag = await stack.start_agent("VM-Win-1")
    assert await wait_for(lambda: ag.registered.is_set())
    async with httpx.AsyncClient(base_url=f"http://{stack.addr}") as c:
        await c.post("/api/login", json={"username": "admin", "password": "pw"})
        r = await c.post("/api/agents/vm-win-1/run-setup")
        assert r.status_code == 200
        r = await c.post("/api/agents/ghost/run-setup")
        assert r.status_code == 409
