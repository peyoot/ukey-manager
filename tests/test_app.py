import httpx
import pytest

from server.app import create_app
from server.config import AppConfig
from server.db import Db
from server.link import Ack
from tests.conftest import FakeLink, make_hub

CFG = {
    "hubs": [
        {"name": "hub-a", "address": "x", "ports": 8, "password_env": "X", "serial": "AW08-A"},
        {"name": "hub-b", "address": "y", "ports": 4, "password_env": "Y", "serial": "AW04-B"},
    ],
    "lease": {"ttl_seconds": 600, "command_ack_timeout_seconds": 0.05, "command_retries": 0},
    "ukeys": [{"hub": "hub-a", "port": 6, "label": "demo disk"}],
}


@pytest.fixture
async def api(tmp_path):
    link = FakeLink()
    hubs = {"hub-a": await make_hub("hub-a", 8, "AW08-A", tmp_path), "hub-b": await make_hub("hub-b", 4, "AW04-B", tmp_path)}
    db = Db(":memory:")
    app = create_app(
        AppConfig.model_validate(CFG), hubs, link, db,
        admin_user="admin", admin_password="s3cret-pass", session_secret="x" * 32,
        cookie_secure=False, background=False,
    )
    sched = app.state.scheduler
    sched.register_agent("vm-win-1", "vm-win-1", "10.0.0.1", "ukey-vm-win-1", auto_approve_ips=["10.0.0.1"])
    sched.register_agent("vm-win-9", "vm-win-9", "10.9.9.9", "ukey-vm-win-9")  # 待批准
    link.online |= {"vm-win-1", "vm-win-9"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, app, link, db


async def login(c, pw="s3cret-pass"):
    return await c.post("/api/login", json={"username": "admin", "password": pw})


async def test_endpoints_require_login(api):
    c, *_ = api
    for method, url in [("GET", "/api/ports"), ("GET", "/api/agents"), ("GET", "/api/audit"), ("GET", "/api/hubs"),
                        ("POST", "/api/ports/hub-a-p06/force-release"), ("POST", "/api/agents/vm-win-9/approve")]:
        assert (await c.request(method, url)).status_code == 401, url


async def test_login_wrong_password_and_cookie_flags(api):
    c, _, _, db = api
    assert (await login(c, "nope")).status_code == 401
    assert (await c.get("/api/ports")).status_code == 401
    r = await login(c)
    assert r.status_code == 200
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    assert (await c.get("/api/session")).json() == {"user": "admin"}
    assert [a["result"] for a in db.list_audit() if a["action"] == "login"] == ["ok", "denied"]


async def test_cookie_is_secure_when_enabled(tmp_path):
    app = create_app(
        AppConfig.model_validate(CFG), {}, FakeLink(), Db(":memory:"),
        admin_user="admin", admin_password="p", session_secret="y" * 32, background=False,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        r = await login(c, "p")
        assert "secure" in r.headers["set-cookie"].lower()


async def test_tampered_session_rejected(api):
    c, *_ = api
    await login(c)
    c.cookies.set("ukey_session", "forged.value.here")
    assert (await c.get("/api/ports")).status_code == 401


async def test_port_lifecycle_over_rest(api):
    c, app, link, db = api
    await login(c)
    ports = (await c.get("/api/ports")).json()
    assert len(ports) == 12 and next(p for p in ports if p["port_id"] == "hub-a-p06")["label"] == "demo disk"
    r = await c.post("/api/ports/hub-a-p06/request", json={"agent_id": "vm-win-1"})
    assert r.status_code == 200 and r.json()["port"]["state"] == "connected"
    hubs = {h["name"]: h for h in (await c.get("/api/hubs")).json()}
    assert hubs["hub-a"]["states"]["connected"] == 1 and hubs["hub-b"]["states"]["connected"] == 0
    r = await c.post("/api/ports/hub-a-p06/force-release")
    assert r.status_code == 200
    assert (await c.get("/api/ports")).json()[5]["state"] == "idle"
    acts = [(a["actor"], a["action"]) for a in (await c.get("/api/audit?limit=3")).json()]
    assert ("admin", "release") in acts


async def test_request_errors_map_to_http_codes(api):
    c, app, link, db = api
    await login(c)
    assert (await c.post("/api/ports/nope/request", json={"agent_id": "vm-win-1"})).status_code == 400
    r = await c.post("/api/ports/hub-a-p06/request", json={"agent_id": "vm-win-9"})  # 未批准
    assert r.status_code == 400 and "not approved" in r.json()["detail"]
    # 占用 → 409 + busy 详情
    link.accept_release = False
    await c.post("/api/ports/hub-a-p06/request", json={"agent_id": "vm-win-1"})
    await c.post("/api/agents/vm-win-9/approve")
    r = await c.post("/api/ports/hub-a-p06/request", json={"agent_id": "vm-win-9"})
    assert r.status_code == 409 and r.json()["detail"]["status"] == "busy"


async def test_agents_listing_hides_token_and_approval(api):
    c, *_ = api
    await login(c)
    agents = {a["agent_id"]: a for a in (await c.get("/api/agents")).json()}
    assert "token_hash" not in agents["vm-win-9"] and agents["vm-win-9"]["status"] == "pending"
    assert agents["vm-win-1"]["status"] == "approved"  # IP 命中 auto_approve_ips
    assert (await c.post("/api/agents/vm-win-9/approve")).status_code == 200
    assert (await c.post("/api/agents/ghost/approve")).status_code == 404
    assert next(a for a in (await c.get("/api/agents")).json() if a["agent_id"] == "vm-win-9")["status"] == "approved"
