"""真实 TCP + WebSocket 端到端：uvicorn 在同一事件循环里起服务，websockets 客户端当代理。"""
import asyncio
import json

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

from common import protocol as P
from common.protocol import MsgType
from server.app import create_app
from server.config import AppConfig
from server.db import Db
from server.ws import AgentHub
from tests.conftest import make_hub

CFG = {
    "hubs": [{"name": "hub-a", "address": "x", "ports": 8, "password_env": "X", "serial": "AW08-A"}],
    "lease": {"ttl_seconds": 600, "command_ack_timeout_seconds": 0.5, "command_retries": 1},
    "ukeys": [{"hub": "hub-a", "port": 6, "label": "demo disk"}],
}


@pytest.fixture
async def live(tmp_path):
    db = Db(":memory:")
    hubs = {"hub-a": await make_hub("hub-a", 8, "AW08-A", tmp_path)}
    link = AgentHub(db, enrollment_token="tok", auto_approve_ips=[], hubs_info=[{"name": "hub-a", "address": "x", "port": 18574, "serial": "AW08-A"}])
    app = create_app(AppConfig.model_validate(CFG), hubs, link, db, admin_user="admin", admin_password="pw",
                     session_secret="z" * 32, cookie_secure=False, background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws="websockets-sansio"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}", db, hubs
    server.should_exit = True
    await asyncio.wait_for(task, 5)


async def rpc(ws, msg_type, payload):
    env = P.make(msg_type, payload)
    await ws.send(P.encode(env))
    while True:
        m = P.decode(await asyncio.wait_for(ws.recv(), 3))
        if m.type == MsgType.ACK and m.payload["ref_id"] == env.id:
            return P.Ack.model_validate(m.payload)


async def test_real_socket_enroll_approve_register_request(live):
    addr, db, hubs = live
    # 1. ENROLL → 待批准
    async with connect(f"ws://{addr}/ws/agent") as ws:
        ack = await rpc(ws, MsgType.ENROLL, P.Enroll(hostname="VM-Win-1", token="tok"))
    assert ack.ok and ack.data["status"] == "pending"
    # 2. 管理员经 REST 登录并批准
    async with httpx.AsyncClient(base_url=f"http://{addr}") as c:
        assert (await c.post("/api/login", json={"username": "admin", "password": "pw"})).status_code == 200
        assert (await c.post("/api/agents/vm-win-1/approve")).status_code == 200
        # 3. 再 ENROLL 拿到专属 token
        async with connect(f"ws://{addr}/ws/agent") as ws:
            ack = await rpc(ws, MsgType.ENROLL, P.Enroll(hostname="VM-Win-1", token="tok"))
        token = ack.data["token"]
        # 4. REGISTER → 收到 CONFIG → 申请端口
        async with connect(f"ws://{addr}/ws/agent") as ws:
            ack = await rpc(ws, MsgType.REGISTER, P.Register(
                agent_id="vm-win-1", token=token, hostname="VM-Win-1", client_id="ukey-vm-win-1",
                manager_mode="service", manager_version="3.1.39.6"))
            assert ack.ok
            cfg = P.decode(await asyncio.wait_for(ws.recv(), 3))
            assert cfg.type == MsgType.CONFIG and cfg.payload["hubs"][0]["serial"] == "AW08-A"
            await ws.send(P.encode(P.ack(cfg.id)))
            req = P.make(MsgType.REQUEST, P.PortRef(label="demo disk"))
            await ws.send(P.encode(req))
            connect_cmd = P.decode(await asyncio.wait_for(ws.recv(), 3))
            assert connect_cmd.type == MsgType.CONNECT and connect_cmd.payload["group"] == "AW08-A.6"
            await ws.send(P.encode(P.ack(connect_cmd.id)))
            result = P.decode(await asyncio.wait_for(ws.recv(), 3))
            assert result.payload["ref_id"] == req.id and result.payload["ok"] is True
            ports = {p["port_id"]: p for p in (await c.get("/api/ports")).json()}
            assert ports["hub-a-p06"]["state"] == "connected"
            assert [a["online"] for a in (await c.get("/api/agents")).json()] == [1]
            # 5. 管理员强制回收 → 代理收到 DISCONNECT
            task = asyncio.create_task(c.post("/api/ports/hub-a-p06/force-release"))
            dis = P.decode(await asyncio.wait_for(ws.recv(), 3))
            assert dis.type == MsgType.DISCONNECT
            await ws.send(P.encode(P.ack(dis.id)))
            assert (await task).status_code == 200
        assert {p["port_id"]: p["state"] for p in (await c.get("/api/ports")).json()}["hub-a-p06"] == "idle"
        assert (await hubs["hub-a"].list_clients())[0].groups == []


async def test_real_socket_unauthenticated_and_garbage(live):
    addr, db, hubs = live
    async with connect(f"ws://{addr}/ws/agent") as ws:
        ack = await rpc(ws, MsgType.REGISTER, P.Register(agent_id="x", token="bad", hostname="x"))
        assert not ack.ok and ack.error == "unauthorized"
    async with connect(f"ws://{addr}/ws/agent") as ws:
        await ws.send("garbage")
        m = P.decode(await asyncio.wait_for(ws.recv(), 3))
        assert m.type == MsgType.ACK and not m.payload["ok"]
    assert (await asyncio.to_thread(lambda: httpx.get(f"http://{addr}/api/ports"))).status_code == 401
