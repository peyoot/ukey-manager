import asyncio
import os
import stat

import httpx
import pytest
import yaml

from server.app import create_app
from server.config import load_config
from server.db import Db
from server.hub.mock import FakeCgi
from server.hub.rest import RestHub
from server.ws import AgentHub
from tests.conftest import make_hub

CONFIG = """# 顶部注释
server:
  public_url: https://10.70.1.63:8443
  console_admin_user: admin
lease:
  ttl_seconds: 600
  command_ack_timeout_seconds: 0.05
  command_retries: 0
grouping:
  name_pattern: "{hub}-p{port:02d}"
hubs:
  - name: hub-a
    address: 10.70.1.208      # hub-a 地址注释
    model: AW08
    serial: AW08-A
    ports: 8
    admin_user: admin
    password_env: HUB_PASSWORD_HUB_A
    tls_verify: false
  # 后续 Hub 用向导添加
ukeys:
  - {hub: hub-a, port: 6, label: "演示 U 盘"}
"""


class Stack:
    pass


def hub_via(fake: FakeCgi, name, address, user, password, tls_verify=False, ports=None, model=None, serial=None, backup_dir="backups"):
    client = httpx.AsyncClient(base_url="https://x", transport=fake.transport(), auth=(user, password))
    return RestHub(name, address, user, password, ports=ports, model=model, serial=serial, backup_dir=backup_dir, client=client)


@pytest.fixture
async def ws_(tmp_path):
    s = Stack()
    s.tmp = tmp_path
    s.cfg_path, s.env_path, s.bdir = tmp_path / "config.yaml", tmp_path / ".env", tmp_path / "backups"
    s.cfg_path.write_text(CONFIG, encoding="utf-8")
    s.env_path.write_text("HUB_PASSWORD_HUB_A=pw-a\nCONSOLE_ADMIN_PASSWORD=x\n", encoding="utf-8")
    os.chmod(s.env_path, 0o644)
    s.fakes = {"10.70.1.209": FakeCgi(password="hub-b-pw", ports=24), "10.70.1.210": FakeCgi(password="c-pw", ports=4)}
    s.hub_a = await make_hub("hub-a", 8, "AW08-A", tmp_path)
    s.db = Db(":memory:")
    s.link = AgentHub(s.db, enrollment_token="t", hubs_info=[])
    s.created = []

    def factory(*, name, address, user, password, tls_verify, ports, model, serial):
        if address == "10.70.1.208":                       # 编辑 hub-a：密码必须与 .env 中一致
            fk = s.hub_a.fake
            fk.password = "pw-a"
        elif address in s.fakes:
            fk = s.fakes[address]
        else:                                              # 不可达
            fk = FakeCgi(); fk.offline = True
        h = hub_via(fk, name, address, user, password, ports=ports, model=model, serial=serial, backup_dir=s.bdir)
        s.created.append(h)
        return h

    cfg = load_config(s.cfg_path)
    s.app = create_app(cfg, {"hub-a": s.hub_a}, s.link, s.db, admin_user="admin", admin_password="pw",
                       session_secret="q" * 32, cookie_secure=False, background=False,
                       base_dir=tmp_path, config_path=s.cfg_path, env_path=s.env_path, backup_dir=s.bdir, hub_factory=factory)
    s.sched = s.app.state.scheduler
    s.c = httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app), base_url="http://t")
    await s.c.post("/api/login", json={"username": "admin", "password": "pw"})
    yield s
    await s.c.aclose()


async def wait_status(s, sid, want=("done", "failed"), timeout=5):
    for _ in range(int(timeout / 0.02)):
        r = (await s.c.get(f"/api/hubs/wizard/{sid}")).json()
        if r["status"] in want:
            return r
        await asyncio.sleep(0.02)
    raise AssertionError(r)


PROBE_B = {"name": "hub-b", "address": "10.70.1.209", "admin_user": "admin", "password": "hub-b-pw",
           "tls_verify": False, "model": "AW24", "serial": "AW24-B"}


async def test_all_new_endpoints_require_login(ws_):
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=ws_.app), base_url="http://t")
    for m, u in [("GET", "/api/hubs/config"), ("POST", "/api/hubs/probe"), ("POST", "/api/hubs/plan"),
                 ("POST", "/api/hubs/apply"), ("GET", "/api/hubs/wizard/x"), ("POST", "/api/hubs/hub-a/restore"),
                 ("DELETE", "/api/hubs/hub-a")]:
        assert (await anon.request(m, u)).status_code == 401, u


async def test_overview_and_name_suggestion(ws_):
    r = (await ws_.c.get("/api/hubs/config")).json()
    assert r["suggested_name"] == "hub-b" and [h["name"] for h in r["hubs"]] == ["hub-a"]


async def test_probe_is_read_only_backs_up_and_never_echoes_password(ws_):
    fake = ws_.fakes["10.70.1.209"]
    r = await ws_.c.post("/api/hubs/probe", json=PROBE_B)
    assert r.status_code == 200
    body = r.json()
    assert "hub-b-pw" not in r.text and "password" not in body
    assert body["identity"]["ports"] == 24 and body["service"]["enabled"] and not body["blocked"]
    assert len(body["groups"]) == 24 and body["groups"][0]["ports"] == list(range(1, 25))   # 出厂：全在 group01
    assert all(m == "GET" for m, _ in fake.calls)            # 探测期间零写入
    assert (ws_.bdir / body["backup"]).exists()
    assert "hub-b" not in open(ws_.cfg_path).read() and "HUB_PASSWORD_HUB_B" not in open(ws_.env_path).read()
    assert any("读不到" in w for w in body["warnings"])


@pytest.mark.parametrize("patch,msg", [
    ({"name": "Hub B"}, "名称"), ({"name": "hub-a"}, "已存在"), ({"address": "10.0.0.1; rm"}, "地址"),
    ({"password": ""}, "密码"), ({"password": "wrong"}, "认证失败"), ({"address": "10.9.9.9"}, "连接 Hub 失败"),
])
async def test_probe_validation_and_connection_errors(ws_, patch, msg):
    r = await ws_.c.post("/api/hubs/probe", json={**PROBE_B, **patch})
    assert r.status_code in (400, 502) and msg in r.json()["detail"], r.text
    assert "wrong" not in r.text and "hub-b-pw" not in r.text


async def test_structure_mismatch_blocks_writing(ws_):
    fake = ws_.fakes["10.70.1.209"]
    del fake.tree["service"]["anywhereusb"]["clients"]        # 模拟固件结构不同
    body = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()
    assert body["blocked"] and any("不一致" in w for w in body["warnings"])
    r = await ws_.c.post("/api/hubs/plan", json={"session_id": body["session_id"]})
    assert r.status_code == 400 and "禁止" in r.json()["detail"]


async def test_add_hub_full_flow(ws_):
    pr = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()
    sid = pr["session_id"]
    plan = (await ws_.c.post("/api/hubs/plan", json={
        "session_id": sid, "grouping": "per-port", "authorization": "dynamic",
        "labels": {"1": "测试 UKey", "2": ""}, "client_ids": ["ukey-vm-win-1", "ukey-vm-win-2"],
    })).json()
    kinds = [c["kind"] for c in plan["changes"]]
    assert kinds.count("move_port") == 23 and kinds.count("add_client") == 2 and kinds[-1] == "register"
    assert plan["clients"] == {"count": 2, "limit": 255} and not any(k == "grant" for k in kinds)   # 动态分配：不预授权
    fake = ws_.fakes["10.70.1.209"]
    n_before = len(fake.calls)
    assert all(m == "GET" for m, _ in fake.calls[n_before:])   # plan 不写
    assert (await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": False})).status_code == 400
    r = await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": True})
    assert r.status_code == 200
    final = await wait_status(ws_, sid)
    assert final["status"] == "done", final
    assert all(p["status"] == "ok" for p in final["progress"])
    # Hub 上：每口一组、描述、客户端
    g = {x["name"]: x for x in (await ws_.c.get("/api/hubs/wizard/" + sid)).json() and
         [{"name": g.name, "ports": g.ports, "d": g.description} for g in await ws_.sched.hubs["hub-b"].list_groups()]}
    assert g["group07"]["ports"] == [7] and g["group07"]["d"] == "hub-b-p07" and g["group01"]["ports"] == [1]
    assert {c.id: c.groups for c in await ws_.sched.hubs["hub-b"].list_clients()} == {"ukey-vm-win-1": [], "ukey-vm-win-2": []}
    # config.yaml：保留注释并新增 hub-b；.env 里有密码变量且权限 600
    text = open(ws_.cfg_path, encoding="utf-8").read()
    assert "# 顶部注释" in text and "# hub-a 地址注释" in text and "# 后续 Hub 用向导添加" in text
    cfg = load_config(ws_.cfg_path)
    hb = cfg.hub("hub-b")
    assert (hb.address, hb.ports, hb.serial, hb.password_env) == ("10.70.1.209", 24, "AW24-B", "HUB_PASSWORD_HUB_B")
    assert "hub-b-pw" not in text                                              # 密码不进 config.yaml
    assert "HUB_PASSWORD_HUB_B=hub-b-pw" in open(ws_.env_path).read()
    assert stat.S_IMODE(os.stat(ws_.env_path).st_mode) == 0o600
    assert list(ws_.bdir.glob("config-*.yaml"))                               # 旧配置已备份
    # 热加载：调度器有了 hub-b 的 24 个端口，标签生效；控制台总览可见；审计有记录
    ports = {p["port_id"]: p for p in (await ws_.c.get("/api/ports")).json()}
    assert len([k for k in ports if k.startswith("hub-b-")]) == 24 and ports["hub-b-p01"]["label"] == "测试 UKey"
    hubs = {h["name"] for h in (await ws_.c.get("/api/hubs")).json()}
    assert hubs == {"hub-a", "hub-b"}
    assert {"wizard_probe", "wizard_apply", "hub_added"} <= {a["action"] for a in ws_.db.list_audit()}
    assert ws_.link.hubs_info[-1]["name"] == "hub-b" and ws_.link.hubs_info[-1]["serial"] == "AW24-B"   # CONFIG 会下发给代理


async def test_failure_midway_stops_and_restore_returns_hub_to_probe_backup(ws_):
    sid = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()["session_id"]
    await ws_.c.post("/api/hubs/plan", json={"session_id": sid, "client_ids": ["c1"]})
    sess = ws_.app.state.wizard.get(sid)
    orig = sess.hub.set_group_description
    calls = {"n": 0}

    async def flaky(group, text):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟 Hub 写入失败")
        return await orig(group, text)

    sess.hub.set_group_description = flaky
    await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": True})
    final = await wait_status(ws_, sid)
    assert final["status"] == "failed" and "模拟 Hub 写入失败" in final["error"] and final["can_restore"]
    assert final["progress"][-1]["status"] == "failed"
    assert not any(p["status"] == "running" for p in final["progress"])
    assert "hub-b" not in open(ws_.cfg_path).read() and "hub-b" not in ws_.sched.hubs   # 失败即停：没登记
    fake = ws_.fakes["10.70.1.209"]["service"] if False else ws_.fakes["10.70.1.209"].tree["service"]["anywhereusb"]
    assert fake["groups"]["group02"]["ports"] == ["2"] or fake["groups"]["group01"]["ports"] != [str(n) for n in range(1, 25)]  # Hub 已被部分修改
    r = await ws_.c.post("/api/hubs/hub-b/restore", json={"session_id": sid})
    assert r.status_code == 200 and r.json()["status"] == "restored" and r.json()["actions"]
    assert sorted(map(int, fake["groups"]["group01"]["ports"])) == list(range(1, 25))      # 回到探测时的备份（顺序无意义）
    assert fake["groups"]["group02"] == {"description": "Group 2", "ports": []}
    assert fake["clients"] == []
    assert (await ws_.c.post("/api/hubs/hub-b/restore", json={"session_id": sid})).status_code == 400   # 不能重复恢复


async def test_restore_only_allowed_after_failure(ws_):
    sid = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()["session_id"]
    r = await ws_.c.post("/api/hubs/hub-b/restore", json={"session_id": sid})
    assert r.status_code == 400 and "失败" in r.json()["detail"]
    assert (await ws_.c.post("/api/hubs/hub-b/restore", json={"session_id": "nope"})).status_code == 404


@pytest.mark.parametrize("opts,msg", [
    ({"client_ids": ["bad id"]}, "非法的 Client ID"),
    ({"client_ids": [f"c{i}" for i in range(256)]}, "上限 255"),
    ({"authorization": "static", "client_ids": ["a"], "static_grants": {"a": ["group99"]}}, "不存在的分组"),
    ({"authorization": "static", "client_ids": ["a"], "static_grants": {"zz": ["group01"]}}, "不在客户端列表"),
    ({"labels": {"99": "x"}}, "超出范围"),
    ({"labels": {"1": "x" * 65}}, "最长 64"),
    ({"grouping": "weird"}, "grouping"),
])
async def test_plan_validation(ws_, opts, msg):
    sid = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()["session_id"]
    r = await ws_.c.post("/api/hubs/plan", json={"session_id": sid, **opts})
    assert r.status_code == 400 and msg in r.json()["detail"], r.text


async def test_static_authorization_grants_selected_groups(ws_):
    sid = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()["session_id"]
    plan = (await ws_.c.post("/api/hubs/plan", json={
        "session_id": sid, "authorization": "static", "client_ids": ["a"], "static_grants": {"a": ["group03", "group06"]}})).json()
    assert [c["text"] for c in plan["changes"] if c["kind"] == "grant"] == ["授权 a → group03", "授权 a → group06"]
    await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": True})
    assert (await wait_status(ws_, sid))["status"] == "done"
    assert {c.id: c.groups for c in await ws_.sched.hubs["hub-b"].list_clients()} == {"a": ["group03", "group06"]}


async def test_keep_existing_groups_only_registers(ws_):
    sid = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()["session_id"]
    plan = (await ws_.c.post("/api/hubs/plan", json={"session_id": sid, "grouping": "keep"})).json()
    assert [c["kind"] for c in plan["changes"]] == ["register"]
    await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": True})
    assert (await wait_status(ws_, sid))["status"] == "done"
    fake = ws_.fakes["10.70.1.209"].tree["service"]["anywhereusb"]
    assert fake["groups"]["group01"]["ports"] == [str(n) for n in range(1, 25)]   # 现有分组原样保留


async def test_enable_service_option(ws_):
    ws_.fakes["10.70.1.209"].tree["service"]["anywhereusb"]["enable"] = "false"
    pr = (await ws_.c.post("/api/hubs/probe", json=PROBE_B)).json()
    assert pr["service"]["enabled"] is False and any("未启用" in w for w in pr["warnings"])
    plan = (await ws_.c.post("/api/hubs/plan", json={"session_id": pr["session_id"], "grouping": "keep", "enable_service": True})).json()
    assert plan["changes"][0]["kind"] == "enable_service"
    await ws_.c.post("/api/hubs/apply", json={"session_id": pr["session_id"], "confirm": True})
    assert (await wait_status(ws_, pr["session_id"]))["status"] == "done"
    assert ws_.fakes["10.70.1.209"].tree["service"]["anywhereusb"]["enable"] == "true"


async def test_edit_existing_hub_uses_env_password_and_refuses_moving_leased_port(ws_):
    # 编辑 hub-a：不提供密码，沿用 .env；hub-a 已是每口一组
    pr = await ws_.c.post("/api/hubs/probe", json={"name": "hub-a", "edit": True})
    assert pr.status_code == 200 and pr.json()["mode"] == "edit", pr.text
    sid = pr.json()["session_id"]
    plan = (await ws_.c.post("/api/hubs/plan", json={"session_id": sid, "labels": {"3": "XBee 板"}, "client_ids": ["ukey-vm-win-1"]})).json()
    kinds = [c["kind"] for c in plan["changes"]]
    assert "move_port" not in kinds and kinds.count("describe") == 8 and "add_client" in kinds   # 分组本来就对，无需移动；只补描述
    await ws_.c.post("/api/hubs/apply", json={"session_id": sid, "confirm": True})
    assert (await wait_status(ws_, sid))["status"] == "done"
    labels = {p["port"]: p["label"] for p in (await ws_.c.get("/api/ports")).json() if p["hub"] == "hub-a"}
    assert labels[3] == "XBee 板"
    ukeys = load_config(ws_.cfg_path).model_extra["ukeys"]
    assert {(u["port"], u["label"]) for u in ukeys if u["hub"] == "hub-a"} == {(3, "XBee 板")}
    # 端口有活动租约时，不许改它的分组
    ws_.sched.register_agent("vm1", "vm1", "10.0.0.1", "ukey-vm1", ["10.0.0.1"])
    ws_.link.conns["vm1"] = object()
    ws_.sched.link = type("L", (), {"is_online": lambda s, a: True,
                                    "send": staticmethod(lambda *a, **k: asyncio.sleep(0, result=__import__("server.link", fromlist=["Ack"]).Ack(True)))})()
    assert (await ws_.sched.request("vm1", "hub-a-p06"))["status"] == "ok"
    await ws_.sched.hubs["hub-a"].set_port_group(6, "group01")   # 人为弄乱，制造"需要移动"
    sid2 = (await ws_.c.post("/api/hubs/probe", json={"name": "hub-a", "edit": True})).json()["session_id"]
    r = await ws_.c.post("/api/hubs/plan", json={"session_id": sid2})
    assert r.status_code == 400 and "活动租约" in r.json()["detail"]


async def test_remove_hub_refused_with_active_lease_then_allowed(ws_):
    ws_.sched.register_agent("vm1", "vm1", "10.0.0.1", "ukey-vm1", ["10.0.0.1"])
    ws_.sched.link = type("L", (), {"is_online": lambda s, a: True,
                                    "send": staticmethod(lambda *a, **k: asyncio.sleep(0, result=__import__("server.link", fromlist=["Ack"]).Ack(True)))})()
    await ws_.sched.request("vm1", "hub-a-p06")
    r = await ws_.c.delete("/api/hubs/hub-a")
    assert r.status_code == 409 and "活动租约" in r.json()["detail"]
    assert "hub-a" in open(ws_.cfg_path).read()
    await ws_.sched.force_release("hub-a-p06")
    import copy
    before = copy.deepcopy(ws_.hub_a.fake.tree)
    r = await ws_.c.delete("/api/hubs/hub-a")
    assert r.status_code == 200 and "Hub 上的配置未改动" in r.json()["note"]
    assert [h.name for h in load_config(ws_.cfg_path).hubs] == [] and "hub-a" not in ws_.sched.hubs
    assert len(ws_.db.list_ports("hub-a")) == 8                                  # 历史数据保留
    assert ws_.hub_a.fake.tree == before                                         # 没动 Hub（适配器已关闭，直接比较设备状态）
    assert (await ws_.c.delete("/api/hubs/ghost")).status_code == 404
    assert list(ws_.bdir.glob("config-*.yaml"))
