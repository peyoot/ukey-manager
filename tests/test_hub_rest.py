import json
import stat

import pytest

from server.hub.base import (
    HubApiError,
    HubAuthError,
    HubUnreachable,
    HubWriteNotAllowed,
    group_name_for_port,
    port_for_group,
    port_id,
)
from server.hub.mock import MockHub
from server.hub.rest import MAX_CLIENTS, parse_snapshot


@pytest.fixture
def hub(tmp_path):
    return MockHub("hub-a", ports=8, backup_dir=tmp_path)


async def groups_of(hub):
    return {g.name: g.ports for g in await hub.list_groups()}


def test_naming_helpers():
    assert group_name_for_port(6) == "group06"
    assert port_for_group("group06") == 6 and port_for_group("custom") is None
    assert port_id("hub-a", 6) == "hub-a-p06"


def test_parse_ignores_empty_array_marker_and_checks_contiguity():
    s = parse_snapshot({"enable": "true", "port": "18574", "groups.group01.description": "G", "": ""})
    assert s.groups["group01"].ports == [] and s.clients == []
    with pytest.raises(HubApiError):
        parse_snapshot({"groups.group01.description": "G", "groups.group01.ports.1": "2"})


async def test_identify_and_status(hub):
    ident = await hub.identify()
    assert ident.ports == 8 and ident.serial == "AW08-MOCK" and ident.verified is False
    st = await hub.service_status()
    assert st.enabled and st.port == 18574 and st.group_count == 8 and st.client_count == 0


async def test_factory_state_all_ports_in_group01(hub):
    g = await groups_of(hub)
    assert g["group01"] == list(range(1, 9)) and g["group02"] == []


async def test_set_port_group_moves_and_is_idempotent(hub):
    assert await hub.set_port_group(6, "group06") is True
    g = await groups_of(hub)
    assert g["group06"] == [6] and 6 not in g["group01"]
    assert await hub.set_port_group(6, "group06") is False  # 幂等


async def test_all_ports_one_group_each_despite_index_shift(hub):
    for p in range(1, 9):
        await hub.set_port_group(p, group_name_for_port(p))
    assert await groups_of(hub) == {group_name_for_port(p): [p] for p in range(1, 9)}


async def test_add_before_remove_never_leaves_port_unassigned(hub):
    await hub.set_port_group(6, "group06")
    appends = [i for i, (m, q) in enumerate(hub.fake.calls) if m == "POST" and q.get("append")]
    deletes = [i for i, (m, q) in enumerate(hub.fake.calls) if m == "DELETE"]
    assert appends and deletes and min(appends) < min(deletes)


async def test_unknown_group_rejected(hub):
    with pytest.raises(HubApiError):
        await hub.set_port_group(6, "group99")


async def test_description(hub):
    assert await hub.set_group_description("group06", "hub-a-p06") is True
    assert await hub.set_group_description("group06", "hub-a-p06") is False
    assert {g.name: g.description for g in await hub.list_groups()}["group06"] == "hub-a-p06"


async def test_client_grant_revoke_lifecycle(hub):
    assert await hub.add_client("ukey-vm-win-1") is True
    assert await hub.add_client("ukey-vm-win-1") is False
    assert await hub.grant("ukey-vm-win-1", "group06") is True
    assert await hub.grant("ukey-vm-win-1", "group06") is False
    await hub.grant("ukey-vm-win-1", "group03")
    assert (await hub.list_clients())[0].groups == ["group06", "group03"]
    assert await hub.revoke("ukey-vm-win-1", "group06") is True  # 序号前移后仍正确
    assert (await hub.list_clients())[0].groups == ["group03"]
    assert await hub.revoke("ukey-vm-win-1", "group06") is False
    assert await hub.remove_client("ukey-vm-win-1") is True
    assert await hub.list_clients() == []


async def test_grant_requires_registered_client_and_existing_group(hub):
    with pytest.raises(HubApiError):
        await hub.grant("nobody", "group06")
    await hub.add_client("c1")
    with pytest.raises(HubApiError):
        await hub.grant("c1", "group99")


async def test_remove_middle_client_keeps_others(hub):
    for c in ("a", "b", "c"):
        await hub.add_client(c)
    await hub.grant("c", "group03")
    await hub.remove_client("b")
    cl = {c.id: c.groups for c in await hub.list_clients()}
    assert cl == {"a": [], "c": ["group03"]}


async def test_client_limit(hub):
    hub.fake.tree["service"]["anywhereusb"]["clients"] = [
        {"id": f"c{i}", "description": "", "groups": []} for i in range(MAX_CLIENTS)
    ]
    with pytest.raises(HubApiError):
        await hub.add_client("one-too-many")


async def test_write_guard_limits_ports(tmp_path):
    hub = MockHub("hub-a", write_ports={6}, backup_dir=tmp_path)
    await hub.set_port_group(6, "group06")
    with pytest.raises(HubWriteNotAllowed):
        await hub.set_port_group(3, "group03")
    await hub.add_client("c1")
    with pytest.raises(HubWriteNotAllowed):
        await hub.grant("c1", "group03")
    assert await hub.grant("c1", "group06") is True


async def test_error_paths(hub):
    hub.fake.password = "changed"
    with pytest.raises(HubAuthError):
        await hub.list_groups()
    h = await hub.health()
    assert h.ok is False and "401" in h.error
    hub.fake.password = "pw"
    assert (await hub.health()).ok is True
    hub.fake.offline = True
    with pytest.raises(HubUnreachable):
        await hub.list_groups()
    assert (await hub.health()).ok is False


async def test_api_error_is_http_200_with_error_flag(hub):
    with pytest.raises(HubApiError, match="invalid path"):
        await hub._read("service.anywhereusb.no_such_key")


async def test_backup_file_is_private_and_has_hub_name(hub):
    p = await hub.backup("t")
    assert json.loads(p.read_text())["hub"] == "hub-a"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "hub-a-t-" in p.name
    assert (await hub.backup("t")) != p  # 同一秒不覆盖


async def test_restore_roundtrip_diff_only(hub):
    await hub.add_client("c1", "vm1")
    await hub.grant("c1", "group01")
    snap = await hub.backup("before")
    # 改乱
    for p in range(1, 9):
        await hub.set_port_group(p, group_name_for_port(p))
    await hub.set_group_description("group06", "x")
    await hub.add_client("c2")
    await hub.grant("c1", "group06")
    await hub.revoke("c1", "group01")
    plan = await hub.restore(snap, dry_run=True)
    assert plan and any("c2" in a for a in plan)
    calls_before = len(hub.fake.calls)
    await hub.restore(snap, dry_run=True)
    assert all(m == "GET" for m, _ in hub.fake.calls[calls_before:])  # dry-run 不写
    await hub.restore(snap)
    assert await groups_of(hub) == {**{f"group0{n}": [] for n in range(2, 9)}, "group01": list(range(1, 9))}
    cl = await hub.list_clients()
    assert [(c.id, c.groups) for c in cl] == [("c1", ["group01"])]
    assert (await hub.restore(snap, dry_run=True)) == []  # 已一致
    assert list(hub.backup_dir.glob("hub-a-pre-restore-*.json"))  # 恢复前自动备份


async def test_restore_rejects_other_hub_backup(tmp_path):
    a = MockHub("hub-a", backup_dir=tmp_path)
    b = MockHub("hub-b", backup_dir=tmp_path)
    p = await a.backup()
    with pytest.raises(HubApiError):
        await b.restore(p)


async def test_two_hubs_are_independent(tmp_path):
    a = MockHub("hub-a", ports=8, backup_dir=tmp_path)
    b = MockHub("hub-b", ports=24, serial="AW24-MOCK", backup_dir=tmp_path)
    await a.set_port_group(6, "group06")
    await b.add_client("c1")
    await b.grant("c1", "group10")
    assert (await b.identify()).ports == 24
    assert (await groups_of(b))["group01"] == list(range(1, 25))
    assert (await groups_of(a))["group06"] == [6]
    assert await a.list_clients() == []
    a.fake.offline = True  # 一台离线不影响另一台
    assert (await a.health()).ok is False and (await b.health()).ok is True
