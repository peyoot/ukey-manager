import asyncio

import pytest

from common import protocol as P
from common.protocol import MsgType
from server.db import Db
from server.scheduler import Scheduler
from server.ws import AgentHub
from tests.conftest import Clock, make_hub
from tests.ws_helpers import FakeWS, MockAgent

ENROLL_TOKEN = "enroll-secret"


class Cfg:
    def __init__(self, name, ports):
        self.name, self.ports = name, ports


@pytest.fixture
async def srv(tmp_path):
    class E:
        pass

    e = E()
    e.clock = Clock()
    e.db = Db(":memory:")
    e.hubs = {"hub-a": await make_hub("hub-a", 8, "AW08-A", tmp_path), "hub-b": await make_hub("hub-b", 4, "AW04-B", tmp_path)}
    e.link = AgentHub(
        e.db, enrollment_token=ENROLL_TOKEN, auto_approve_ips=["10.0.0.99"],
        hubs_info=[{"name": "hub-a", "address": "10.70.1.208", "port": 18574, "serial": "AW08-A"}],
        heartbeat_interval=15, idle_timeout=5,
    )
    e.sched = Scheduler(e.db, e.hubs, e.link, lease_ttl=600, heartbeat_timeout=45, ack_timeout=0.15, retries=2, clock=e.clock)
    e.link.bind(e.sched)
    e.sched.sync_ports([Cfg("hub-a", 8), Cfg("hub-b", 4)], [{"hub": "hub-a", "port": 6, "label": "demo disk"}])

    async def enroll(hostname, ip="10.0.0.1", token=ENROLL_TOKEN):
        ws = FakeWS(ip)
        task = asyncio.create_task(e.link.handle(ws))
        await ws.say(P.make(MsgType.ENROLL, P.Enroll(hostname=hostname, ip="1.1.1.1", token=token)))
        reply = await ws.hear()
        await asyncio.wait_for(task, 2)
        return ws, P.Ack.model_validate(reply.payload)

    async def agent(name, **kw):
        ip = kw.pop("ip", "10.0.0.99")   # 默认命中 auto_approve_ips
        _, a = await enroll(name, ip=ip)
        return await MockAgent(e.link, name, a.data["token"], ip=ip, **kw).start()

    e.enroll, e.agent = enroll, agent
    e.agents = []
    yield e
    for a in e.agents:
        await a.stop()


async def grants(hub):
    return {c.id: c.groups for c in await hub.list_clients()}


async def test_enroll_bad_token_rejected_and_audited(srv):
    ws, ack = await srv.enroll("vm-x", token="wrong")
    assert not ack.ok and ws.closed_code == 4401
    assert srv.db.get_agent("vm-x") is None
    assert srv.db.list_audit()[0]["action"] == "enroll" and srv.db.list_audit()[0]["result"] == "denied"


async def test_enroll_pending_then_approved_issues_token_once(srv):
    ws, ack = await srv.enroll("vm-new", ip="10.0.0.5")
    assert ack.ok and ack.data["status"] == "pending" and "token" not in ack.data
    assert srv.db.get_agent("vm-new")["ip"] == "10.0.0.5"  # 源地址，不是自报的 1.1.1.1
    _, again = await srv.enroll("vm-new", ip="10.0.0.5")
    assert again.data["status"] == "pending"
    assert srv.sched.approve_agent("vm-new")
    _, ack2 = await srv.enroll("vm-new", ip="10.0.0.5")
    assert ack2.ok and ack2.data["status"] == "approved" and len(ack2.data["token"]) > 20
    stored = srv.db.get_agent("vm-new")["token_hash"]
    assert stored and ack2.data["token"] not in stored  # 库里只存哈希
    _, ack3 = await srv.enroll("vm-new", ip="10.0.0.5")
    assert not ack3.ok and "already enrolled" in ack3.error
    srv.db.set_agent("vm-new", token_hash=None)  # 管理员重置
    _, ack4 = await srv.enroll("vm-new", ip="10.0.0.5")
    assert ack4.ok and ack4.data["token"] != ack2.data["token"]


async def test_enroll_auto_approve_by_ip_and_rejected_agent(srv):
    _, ack = await srv.enroll("vm-auto", ip="10.0.0.99")
    assert ack.data["status"] == "approved" and ack.data["token"]
    srv.db.upsert_agent("vm-bad", "vm-bad", "10.0.0.7", None, "rejected")
    _, ack = await srv.enroll("vm-bad", ip="10.0.0.7")
    assert not ack.ok and "rejected" in ack.error


async def test_register_requires_valid_token(srv):
    _, a = await srv.enroll("vm-1", ip="10.0.0.99")
    bad = MockAgent(srv.link, "vm-1", "not-the-token")
    await bad.start()
    assert bad._first.ok is False and bad.ws.closed_code == 4401
    unknown = MockAgent(srv.link, "ghost", a.data["token"])
    await unknown.start()
    assert unknown._first.ok is False
    assert not srv.link.is_online("vm-1") and not srv.link.is_online("ghost")


async def test_register_ok_pushes_config_and_marks_online(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    assert srv.link.is_online("vm-1")
    await asyncio.sleep(0.05)
    cfg = next(e for e in ag.received if e.type == MsgType.CONFIG)
    c = P.Config.model_validate(cfg.payload)
    assert c.hubs[0].serial == "AW08-A" and c.hubs[0].address == "10.70.1.208" and c.heartbeat_interval_seconds == 15
    row = srv.db.get_agent("vm-1")
    assert row["online"] == 1 and row["client_id"] == "ukey-vm-1" and row["ip"] == "10.0.0.99"
    assert '"manager_mode": "service"' in row["info"]


async def test_full_request_release_cycle_over_protocol(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    ack = await ag.request(port_id="hub-a-p06")
    assert ack.ok and ack.data["status"] == "ok"
    conn = next(e for e in ag.received if e.type == MsgType.CONNECT)
    assert conn.payload == {"port_id": "hub-a-p06", "group": "AW08-A.6", "hub": "hub-a"}
    assert (await grants(srv.hubs["hub-a"]))["ukey-vm-1"] == ["group06"]
    assert srv.db.get_port("hub-a-p06")["state"] == "connected"
    rel = await ag.release("hub-a-p06")
    assert rel.ok
    assert MsgType.DISCONNECT in ag.types()
    assert (await grants(srv.hubs["hub-a"]))["ukey-vm-1"] == []
    assert srv.db.get_port("hub-a-p06")["state"] == "idle"


async def test_request_by_label_and_unknown_label(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    assert (await ag.request(label="demo disk")).data["port_id"] == "hub-a-p06"
    bad = await ag.request(label="nope")
    assert not bad.ok and "unknown" in bad.error


async def test_contention_over_protocol_transfers_after_release_ask(srv):
    a1, a2 = await srv.agent("vm-1"), await srv.agent("vm-2")
    srv.agents += [a1, a2]
    await a1.request(port_id="hub-a-p06")
    ack = await a2.request(port_id="hub-a-p06")
    assert ack.ok
    ask = next(e for e in a1.received if e.type == MsgType.RELEASE_ASK)
    assert ask.payload["requester"] == "vm-2" and ask.payload["port_id"] == "hub-a-p06"
    assert a1.types()[-2:] == [MsgType.RELEASE_ASK, MsgType.DISCONNECT]
    assert MsgType.CONNECT in a2.types()
    assert srv.db.get_port("hub-a-p06")["agent_id"] == "vm-2"


async def test_contention_declined_returns_busy_to_requester(srv):
    a1, a2 = await srv.agent("vm-1"), await srv.agent("vm-2")
    srv.agents += [a1, a2]
    a1.accept_release = False
    await a1.request(port_id="hub-a-p06")
    ack = await a2.request(port_id="hub-a-p06")
    assert not ack.ok and ack.data["status"] == "busy" and ack.data["holder"] == "vm-1"


async def test_command_retry_resends_same_id_and_agent_executes_once(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    ag.drop_first.add(MsgType.CONNECT)  # 第一次丢包
    ack = await ag.request(port_id="hub-a-p06")
    assert ack.ok
    connects = [e for e in ag.received if e.type == MsgType.CONNECT]
    assert len(connects) == 2 and connects[0].id == connects[1].id  # 重发，id 不变
    assert len([i for i in ag.executed if i == connects[0].id]) == 1  # 只执行一次


async def test_command_without_ack_fails_after_retries_and_rolls_back(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    # 这个代理永远不 ACK CONNECT：在发送端吞掉对 CONNECT 的 ACK
    orig = ag.ws.say

    async def say(env, orig=orig):
        if not isinstance(env, str) and env.type == MsgType.ACK and P.Ack.model_validate(env.payload).ref_id in {
            e.id for e in ag.received if e.type == MsgType.CONNECT}:
            return
        await orig(env)

    ag.ws.say = say
    ack = await ag.request(port_id="hub-a-p06")
    assert not ack.ok and "timeout" in ack.error
    assert srv.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(srv.hubs["hub-a"]))["ukey-vm-1"] == []
    assert len([e for e in ag.received if e.type == MsgType.CONNECT]) == 3  # 1 次 + 2 次重试


async def test_duplicate_agent_message_id_replays_reply_without_reexecuting(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    a1 = await ag.call(MsgType.REQUEST, P.PortRef(port_id="hub-a-p06"), id="fixed-id")
    n_connect = ag.types().count(MsgType.CONNECT)
    a2 = await ag.call(MsgType.REQUEST, P.PortRef(port_id="hub-a-p06"), id="fixed-id")
    assert a1.model_dump() == a2.model_dump()
    assert ag.types().count(MsgType.CONNECT) == n_connect


async def test_protocol_violation_closes_connection(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    await ag.ws.say("{ not json")
    await asyncio.sleep(0.1)
    assert ag.ws.closed_code == 4400
    assert not srv.link.is_online("vm-1")


async def test_agent_cannot_send_server_only_messages(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    ack = await ag.call(MsgType.CONNECT, P.GroupCommand(port_id="hub-a-p06", group="AW08-A.6", hub="hub-a"))
    assert not ack.ok and "unexpected" in ack.error
    assert srv.db.get_port("hub-a-p06")["state"] == "idle"


async def test_reconnect_replaces_old_connection(srv):
    _, a = await srv.enroll("vm-1", ip="10.0.0.99")
    old = await MockAgent(srv.link, "vm-1", a.data["token"]).start()
    new = await MockAgent(srv.link, "vm-1", a.data["token"]).start()
    srv.agents += [new]
    await asyncio.sleep(0.05)
    assert old.ws.closed_code == 4000 and srv.link.is_online("vm-1")
    assert (await new.request(port_id="hub-a-p06")).ok  # 新连接可用


async def test_disconnect_midway_fails_pending_command_fast(srv):
    ag = await srv.agent("vm-1")
    ag.drop_first.add(MsgType.CONNECT)
    t = asyncio.create_task(srv.sched.request("vm-1", "hub-a-p06"))
    await asyncio.sleep(0.05)
    ag.ws.disconnect()
    res = await asyncio.wait_for(t, 2)
    assert res["status"] == "error" and "disconnected" in res["error"]
    assert srv.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(srv.hubs["hub-a"]))["ukey-vm-1"] == []


async def test_crash_then_heartbeat_timeout_triggers_server_revoke(srv):
    ag = await srv.agent("vm-1")
    await ag.request(port_id="hub-a-p06")
    await ag.heartbeat(["AW08-A.6"])
    ag.ws.disconnect()  # 代理崩溃
    await asyncio.sleep(0.05)
    assert not srv.link.is_online("vm-1")
    await srv.sched.sweep()  # 还没超时：保持租约
    assert srv.db.get_port("hub-a-p06")["state"] == "connected"
    srv.clock.advance(46)
    await srv.sched.sweep()
    assert srv.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(srv.hubs["hub-a"]))["ukey-vm-1"] == []
    assert srv.db.list_leases()[0]["end_reason"] == "agent_lost"


async def test_manager_not_in_service_mode_cannot_request(srv):
    ag = await srv.agent("vm-1", mode="standalone")
    srv.agents.append(ag)
    ack = await ag.request(port_id="hub-a-p06")
    assert not ack.ok and "manager not ready" in ack.error


async def test_run_setup_and_notify_need_ack(srv):
    ag = await srv.agent("vm-1")
    srv.agents.append(ag)
    assert (await srv.link.run_setup("vm-1")).ok
    assert (await srv.link.notify("vm-1", "hello")).ok
    assert MsgType.RUN_SETUP in ag.types() and MsgType.NOTIFY in ag.types()
    assert (await srv.link.run_setup("offline-agent")).error == "agent offline"
