import subprocess
from pathlib import Path

import pytest

from agent.manager_cli import ManagerCli, parse_list

FIX = Path(__file__).parent / "fixtures"


def test_parse_real_connected_output():
    info = parse_list((FIX / "list_full_connected.txt").read_text())
    assert info.running and info.as_service and info.version == "3.1.39.6"
    assert info.client_id == "ukey-vm-win-1" and info.autofind is False
    hub = info.hubs[0]
    assert (hub.address, hub.serial, hub.model, hub.firmware, hub.state) == (
        "10.70.1.208:18574", "AW08-017363", "AnywhereUSB 8 Plus", "26.2.148.166", "Active (secure)")
    assert [g.address for g in hub.groups] == ["AW08-017363.2", "AW08-017363.3", "AW08-017363.6"]
    g3 = hub.groups[1]
    assert (g3.name, g3.number, g3.ports, g3.in_use_by, g3.in_use_by_you) == ("hub-a-p03", 3, [3], "NO ONE", False)
    xbee = g3.devices[0]
    assert (xbee.name, xbee.address, xbee.vendor, xbee.port_on_hub) == (
        "XBee Micro Development Board", "AW08-017363.1703", "Digi", 3)
    g6 = hub.groups[2]
    assert g6.in_use_by_you and g6.in_use_by == "YOU"
    disk = g6.devices[0]
    assert (disk.name, disk.serial, disk.port_on_hub, disk.in_use_by_you) == ("Disk 20", "FC1575EA0B448", 6, True)
    assert hub.groups[0].devices == []  # 空分组没有设备行
    assert info.connected_groups() == ["AW08-017363.6"]


def test_parse_no_hubs_and_empty_client_id():
    info = parse_list((FIX / "list_full_empty_standalone.txt").read_text())
    assert info.running and info.hubs == [] and info.client_id == "" and info.autofind is True
    assert info.connected_groups() == []


def test_parse_not_running():
    info = parse_list("ERROR: AnywhereUSB Manager is not running.\n")
    assert info.running is False and info.hubs == [] and not info.as_service


def test_parse_tolerates_garbage_and_wrapped_lines():
    text = (FIX / "list_full_connected.txt").read_text().replace("  PORT: 18574", "  PORT: 18574 \x00")
    assert parse_list(text).hubs[0].groups  # 不因杂字符崩溃
    assert parse_list("").hubs == []


class FakeRun:
    def __init__(self, rc=0, out="OK\n"):
        self.calls, self.rc, self.out = [], rc, out

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, self.out, "")


def test_commands_use_arg_lists_and_documented_syntax():
    r = FakeRun()
    m = ManagerCli("C:/x/awusbmanager.exe", runner=r)
    assert m.connect_group("AW08-017363.6").ok
    assert r.calls[-1][0] == ["C:/x/awusbmanager.exe", "CONNECT", "GROUP,AW08-017363.6"]
    m.disconnect_group("AW08-017363.6")
    assert r.calls[-1][0][1:] == ["DISCONNECT", "GROUP,AW08-017363.6"]
    m.known_hub_add("10.70.1.208")
    assert r.calls[-1][0][1:] == ["KNOWN", "HUB", "ADD,10.70.1.208"]
    m.autofind_off()
    assert r.calls[-1][0][1:] == ["AUTOFIND,OFF"]
    m.set_client_id("ukey-vm-win-1")
    assert r.calls[-1][0][1:] == ["SET", "CLIENTID,ukey-vm-win-1"]
    assert all(kw.get("shell") is None for _, kw in r.calls)  # 不经 shell


@pytest.mark.parametrize("bad", ["AW08;calc", "a b", "x&y", "", "$(id)", "a|b", "../x", "a" * 80])
def test_rejects_injection_like_addresses(bad):
    m = ManagerCli("x", runner=FakeRun())
    with pytest.raises(ValueError):
        m.connect_group(bad)
    with pytest.raises(ValueError):
        m.set_client_id(bad)


def test_failed_and_timeout_and_missing_exe():
    assert not ManagerCli("x", runner=FakeRun(1, "FAILED\n")).connect_group("AW.1").ok
    assert not ManagerCli("x", runner=FakeRun(0, "weird")).connect_group("AW.1").ok

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)

    assert ManagerCli("x", runner=boom).connect_group("AW.1").rc == 124

    def missing(*a, **k):
        raise FileNotFoundError

    assert ManagerCli("x", runner=missing).info().running is True  # 空输出 → 无 Hub，不崩溃
