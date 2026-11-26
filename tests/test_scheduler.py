import asyncio

import pytest

from server.hub.base import HubError


async def grants(hub):
    return {c.id: c.groups for c in await hub.list_clients()}


async def test_sync_ports_and_labels(env):
    ports = env.db.list_ports()
    assert len(ports) == 12 and {p["hub"] for p in ports} == {"hub-a", "hub-b"}
    p6 = env.db.get_port("hub-a-p06")
    assert p6["grp"] == "group06" and p6["label"] == "demo disk" and p6["state"] == "idle"


async def test_sync_ports_warns_when_hub_removed_but_keeps_data(env):
    class Cfg:
        name, ports = "hub-a", 8

    warnings = env.sched.sync_ports([Cfg()])
    assert any("hub-b" in w for w in warnings)
    assert len(env.db.list_ports("hub-b")) == 4


async def test_request_idle_port_grants_and_connects(env):
    r = await env.sched.request("vm-win-1", "hub-a-p06")
    assert r["status"] == "ok"
    p = env.db.get_port("hub-a-p06")
    assert (p["state"], p["client_id"], p["agent_id"]) == ("connected", "ukey-vm-win-1", "vm-win-1")
    assert await grants(env.hub_a) == {"ukey-vm-win-1": ["group06"]}
    agent, typ, payload = env.link.sent[-1]
    assert (agent, typ) == ("vm-win-1", "CONNECT") and payload["group"] == "AW08-A.6"
    assert env.db.active_lease("hub-a-p06")["expires_at"] == env.clock() + 600
    assert [a["action"] for a in reversed(env.db.list_audit())][-2:] == ["grant", "connect"]


async def test_request_validations(env):
    assert (await env.sched.request("vm-win-1", "nope"))["error"] == "unknown port"
    assert (await env.sched.request("ghost", "hub-a-p06"))["error"] == "unknown agent"
    env.db.upsert_agent("vm-3", "vm-3", "10.9.9.9", "ukey-vm-3")
    env.link.online.add("vm-3")
    env.db.set_agent("vm-3", online=1)
    assert (await env.sched.request("vm-3", "hub-a-p06"))["error"] == "agent not approved"
    env.link.online.discard("vm-win-1")
    assert (await env.sched.request("vm-win-1", "hub-a-p06"))["error"] == "agent offline"
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert await grants(env.hub_a) == {}


async def test_same_agent_request_is_idempotent_and_extends(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.clock.advance(100)
    r = await env.sched.request("vm-win-1", "hub-a-p06")
    assert r["already"] is True
    assert env.db.get_port("hub-a-p06")["lease_expires_at"] == env.clock() + 600
    assert env.link.types().count("CONNECT") == 1


async def test_contention_declined_returns_busy(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.link.accept_release = False
    r = await env.sched.request("vm-win-2", "hub-a-p06")
    assert r == {"status": "busy", "holder": "vm-win-1", "asked": True}
    assert env.db.get_port("hub-a-p06")["agent_id"] == "vm-win-1"
    assert "RELEASE_ASK" in env.link.types("vm-win-1")


async def test_contention_accepted_transfers_port(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    r = await env.sched.request("vm-win-2", "hub-a-p06")
    assert r["status"] == "ok"
    assert env.db.get_port("hub-a-p06")["agent_id"] == "vm-win-2"
    assert await grants(env.hub_a) == {"ukey-vm-win-1": [], "ukey-vm-win-2": ["group06"]}
    types = env.link.types()
    # 顺序：CONNECT(1) → RELEASE_ASK(1) → DISCONNECT(1) → CONNECT(2)
    assert types[-4:] == ["CONNECT", "RELEASE_ASK", "DISCONNECT", "CONNECT"]
    reasons = [l["end_reason"] for l in env.db.list_leases()]
    assert reasons == ["yielded", None]


async def test_contention_holder_unresponsive_stays_busy(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.link.drop.add("vm-win-1")
    r = await env.sched.request("vm-win-2", "hub-a-p06")
    assert r["status"] == "busy"
    assert await grants(env.hub_a) == {"ukey-vm-win-1": ["group06"]}


async def test_release_graceful_order_and_holder_check(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    assert (await env.sched.release("vm-win-2", "hub-a-p06"))["error"] == "not the holder"
    assert (await env.sched.release("vm-win-1", "hub-a-p06"))["status"] == "ok"
    assert env.link.types()[-1] == "DISCONNECT"
    p = env.db.get_port("hub-a-p06")
    assert (p["state"], p["client_id"], p["agent_id"]) == ("idle", None, None)
    assert (await grants(env.hub_a))["ukey-vm-win-1"] == []
    assert env.db.list_leases()[0]["end_reason"] == "released"
    assert (await env.sched.release("vm-win-1", "hub-a-p06"))["already"] is True


async def test_force_release_even_when_agent_is_gone(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.link.online.discard("vm-win-1")
    r = await env.sched.force_release("hub-a-p06", actor="admin")
    assert r["status"] == "ok"
    assert (await grants(env.hub_a))["ukey-vm-win-1"] == []
    last = env.db.list_audit()[0]
    assert (last["actor"], last["action"], last["result"]) == ("admin", "release", "ok")


async def test_connect_failure_rolls_back(env):
    env.link.fail_types.add("CONNECT")
    r = await env.sched.request("vm-win-1", "hub-a-p06")
    assert r["status"] == "error" and "connect failed" in r["error"]
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(env.hub_a))["ukey-vm-win-1"] == []
    assert env.db.list_leases()[0]["end_reason"] == "connect_failed"


async def test_hub_failure_during_grant_leaves_no_lease(env):
    env.hub_a.fake.offline = True
    r = await env.sched.request("vm-win-1", "hub-a-p06")
    assert r["status"] == "error" and r["error"].startswith("hub:")
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert env.db.list_leases()[0]["end_reason"] == "grant_failed"
    assert "CONNECT" not in env.link.types()


async def test_fallback_heartbeat_timeout_revokes_directly(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    await env.sched.on_heartbeat("vm-win-1", ["AW08-A.6"])
    env.clock.advance(46)  # 超过 heartbeat_timeout=45
    env.link.sent.clear()
    await env.sched.sweep()
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(env.hub_a))["ukey-vm-win-1"] == []
    assert "DISCONNECT" not in env.link.types()  # 失联：不等代理，直接 revoke
    assert env.db.list_leases()[0]["end_reason"] == "agent_lost"
    assert env.db.get_agent("vm-win-1")["online"] == 0
    actions = [a["action"] for a in env.db.list_audit()]
    assert "agent_timeout" in actions and "release" in actions


async def test_fallback_lease_expiry_with_unresponsive_agent(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.clock.advance(599)
    await env.sched.on_heartbeat("vm-win-1", ["AW08-A.6"])
    await env.sched.sweep()
    assert env.db.get_port("hub-a-p06")["state"] == "connected"  # 还没到期
    env.clock.advance(2)
    await env.sched.on_heartbeat("vm-win-1", ["AW08-A.6"])
    env.link.drop.add("vm-win-1")  # 在线但不响应 DISCONNECT
    await env.sched.sweep()
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert (await grants(env.hub_a))["ukey-vm-win-1"] == []
    assert env.db.list_leases()[0]["end_reason"] == "expired"
    assert "DISCONNECT" in env.link.types()


async def test_renew_prevents_expiry(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.clock.advance(500)
    assert (await env.sched.renew("vm-win-1", "hub-a-p06"))["status"] == "ok"
    env.clock.advance(500)
    await env.sched.on_heartbeat("vm-win-1", ["AW08-A.6"])
    await env.sched.sweep()
    assert env.db.get_port("hub-a-p06")["state"] == "connected"


async def test_heartbeat_without_group_ends_lease(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.clock.advance(1)  # 过了 2*ack_timeout 的宽限
    await env.sched.on_heartbeat("vm-win-1", [])  # 本机已不再连接该分组
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert env.db.list_leases()[0]["end_reason"] == "agent_disconnected"


async def test_revoke_failure_marks_fault_not_idle(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.hub_a.fake.offline = True
    r = await env.sched.force_release("hub-a-p06")
    assert r["status"] == "error"
    p = env.db.get_port("hub-a-p06")
    assert p["state"] == "fault" and "revoke failed" in p["fault_reason"]


async def test_hub_offline_only_affects_its_own_ports_and_recovers(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    env.hub_b.fake.offline = True
    await env.sched.check_hubs()
    assert {p["state"] for p in env.db.list_ports("hub-b")} == {"fault"}
    assert env.db.get_port("hub-a-p06")["state"] == "connected"
    assert (await env.sched.request("vm-win-2", "hub-b-p01"))["error"].startswith("port fault")
    env.hub_b.fake.offline = False
    await env.sched.check_hubs()
    assert {p["state"] for p in env.db.list_ports("hub-b")} == {"idle"}
    assert (await env.sched.request("vm-win-2", "hub-b-p01"))["status"] == "ok"


async def test_hub_offline_with_lease_recovers_connected_when_grant_still_there(env):
    await env.sched.request("vm-win-2", "hub-b-p02")
    env.hub_b.fake.offline = True
    await env.sched.check_hubs()
    assert env.db.get_port("hub-b-p02")["state"] == "fault"
    env.hub_b.fake.offline = False
    await env.sched.check_hubs()
    p = env.db.get_port("hub-b-p02")
    assert (p["state"], p["agent_id"]) == ("connected", "vm-win-2")


async def test_reconcile_orphan_grant_warns_then_auto_revokes(env):
    await env.hub_a.add_client("stranger")
    await env.hub_a.grant("stranger", "group03")
    findings = await env.sched.reconcile("hub-a")
    assert any("stranger" in f and "group03" in f for f in findings)
    assert (await grants(env.hub_a))["stranger"] == ["group03"]  # 默认只告警
    env.sched.auto_revoke_orphans = True
    await env.sched.reconcile("hub-a")
    assert (await grants(env.hub_a))["stranger"] == []


async def test_reconcile_static_mode_ignores_standing_grants(env):
    env.sched.authorization_mode = "static"
    await env.hub_a.add_client("static-c")
    await env.hub_a.grant("static-c", "group03")
    assert await env.sched.reconcile("hub-a") == []


async def test_reconcile_lease_without_grant_is_ended(env):
    await env.sched.request("vm-win-1", "hub-a-p06")
    await env.hub_a.revoke("ukey-vm-win-1", "group06")  # 有人在 Hub 上手工撤销
    findings = await env.sched.reconcile("hub-a")
    assert any("无授权" in f for f in findings)
    assert env.db.get_port("hub-a-p06")["state"] == "idle"
    assert env.db.list_leases()[0]["end_reason"] == "grant_missing"


async def test_two_agents_two_hubs_in_parallel(env):
    r = await asyncio.gather(
        env.sched.request("vm-win-1", "hub-a-p06"),
        env.sched.request("vm-win-2", "hub-b-p01"),
    )
    assert [x["status"] for x in r] == ["ok", "ok"]
    assert await grants(env.hub_a) == {"ukey-vm-win-1": ["group06"]}
    assert await grants(env.hub_b) == {"ukey-vm-win-2": ["group01"]}


async def test_concurrent_requests_same_port_only_one_wins(env):
    env.link.accept_release = False
    r = await asyncio.gather(
        env.sched.request("vm-win-1", "hub-a-p03"),
        env.sched.request("vm-win-2", "hub-a-p03"),
    )
    assert sorted(x["status"] for x in r) == ["busy", "ok"]
    holders = [c for c, g in (await grants(env.hub_a)).items() if g == ["group03"]]
    assert len(holders) == 1


async def test_subscribers_get_events_and_errors_are_isolated(env):
    seen = []
    env.sched.subscribe(lambda e: 1 / 0)  # 出错的订阅者不能影响调度
    env.sched.subscribe(seen.append)
    await env.sched.request("vm-win-1", "hub-a-p06")
    assert {"event": "port", "port_id": "hub-a-p06"} in seen
