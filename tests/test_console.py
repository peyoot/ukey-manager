"""控制台：静态页面与安全头、/ws/console 实时推送与认证、页面引用的接口都存在。"""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed

from server.app import create_app
from server.config import AppConfig
from server.db import Db
from tests.conftest import FakeLink, make_hub

CFG = {"hubs": [{"name": "hub-a", "address": "x", "ports": 8, "password_env": "X", "serial": "AW08-A"}],
       "lease": {"command_ack_timeout_seconds": 0.2, "command_retries": 0}}


@pytest.fixture
async def live(tmp_path):
    db, link = Db(":memory:"), FakeLink()
    hubs = {"hub-a": await make_hub("hub-a", 8, "AW08-A", tmp_path)}
    app = create_app(AppConfig.model_validate(CFG), hubs, link, db, admin_user="admin", admin_password="pw",
                     session_secret="c" * 32, cookie_secure=False, background=False, base_dir=tmp_path)
    sched = app.state.scheduler
    sched.register_agent("vm1", "vm1", "10.0.0.1", "ukey-vm1", ["10.0.0.1"], {"manager_mode": "service"})
    link.online.add("vm1")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    addr = f"127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    yield addr, sched
    server.should_exit = True
    await asyncio.wait_for(task, 5)


async def login_cookie(addr):
    async with httpx.AsyncClient(base_url=f"http://{addr}") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        return r.headers["set-cookie"].split(";")[0]


async def test_static_page_and_security_headers(live):
    addr, _ = live
    async with httpx.AsyncClient(base_url=f"http://{addr}") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "UKey 管理控制台" in r.text
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert (await c.get("/static/app.js")).status_code == 200 and (await c.get("/static/style.css")).status_code == 200
        assert "content-security-policy" not in (await c.get("/api/session")).headers        # API 不需要 CSP
        assert "script" not in re.sub(r'<script src="/static/app\.js"></script>', "", r.text)  # 页面里没有内联脚本


def test_page_has_no_inline_script_style_or_handlers():
    html = Path("server/static/index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "style=" not in html and not re.search(r"\son\w+=", html)
    js = Path("server/static/app.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert bad not in code, bad
    # 页面调用的 API 路径都存在于服务器路由里
    from server.app import create_app as _  # noqa: F401


async def test_every_api_path_used_by_the_page_exists(live):
    addr, _ = live
    js = Path("server/static/app.js").read_text(encoding="utf-8")
    paths = set(re.findall(r'"/api/([\w/-]+)', js)) | set(re.findall(r"`/api/([\w/-]+)", js))
    cookie = await login_cookie(addr)
    async with httpx.AsyncClient(base_url=f"http://{addr}", headers={"cookie": cookie}) as c:
        for p in sorted(paths):
            if "$" in p or p.endswith("/"):
                continue
            r = await c.get("/api/" + p)
            assert r.status_code != 404 or p.startswith("hubs/wizard"), p     # 404 = 路由不存在（向导会话 404 是正常业务错误）


async def test_console_ws_requires_login_and_same_origin(live):
    addr, _ = live
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        async with connect(f"ws://{addr}/ws/console") as ws:
            await asyncio.wait_for(ws.recv(), 2)
    cookie = await login_cookie(addr)
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        async with connect(f"ws://{addr}/ws/console", additional_headers={"Cookie": cookie, "Origin": "http://evil.example"}) as ws:
            await asyncio.wait_for(ws.recv(), 2)
    async with connect(f"ws://{addr}/ws/console", additional_headers={"Cookie": cookie, "Origin": f"http://{addr}"}) as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"event": "hello"}


async def test_console_ws_pushes_port_and_agent_events(live):
    addr, sched = live
    cookie = await login_cookie(addr)
    async with connect(f"ws://{addr}/ws/console", additional_headers={"Cookie": cookie}) as ws, \
            connect(f"ws://{addr}/ws/console", additional_headers={"Cookie": cookie}) as ws2:
        await ws.recv(), await ws2.recv()
        await sched.request("vm1", "hub-a-p06")
        ev = json.loads(await asyncio.wait_for(ws.recv(), 2))
        assert ev == {"event": "port", "port_id": "hub-a-p06"}
        assert json.loads(await asyncio.wait_for(ws2.recv(), 2)) == ev          # 多个控制台都收到
        await sched.force_release("hub-a-p06")
        assert json.loads(await asyncio.wait_for(ws.recv(), 2))["port_id"] == "hub-a-p06"
        sched.approve_agent("vm1")
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"event": "agent", "agent_id": "vm1"}


async def test_closed_console_ws_is_cleaned_up(live):
    addr, sched = live
    cookie = await login_cookie(addr)
    async with connect(f"ws://{addr}/ws/console", additional_headers={"Cookie": cookie}) as ws:
        await ws.recv()
    await asyncio.sleep(0.1)
    await sched.request("vm1", "hub-a-p06")      # 已断开的订阅者不会让调度出错
    assert sched.db.get_port("hub-a-p06")["state"] == "connected"
