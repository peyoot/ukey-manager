import hashlib

import pytest

from agent.manager_mock import MockManager, MockSystem
from agent.manager_setup import apply_hubs, detect_state, setup_manager

HUBS = [{"name": "hub-a", "address": "10.70.1.208", "port": 18574, "serial": "AW08-017363"}]


def run(system, mgr, **kw):
    kw.setdefault("hostname", "VM-Win-1")
    return setup_manager(system, mgr, **kw)


def names(rep):
    return [(s.name, s.ok) for s in rep.steps]


def test_detect_states():
    assert detect_state(MockSystem("missing")) == "missing"
    assert detect_state(MockSystem("standalone")) == "standalone"
    assert detect_state(MockSystem("service")) == "service"


def test_already_service_sets_client_id_and_hubs_idempotently():
    sysm, mgr = MockSystem("service"), MockManager(client_id="")
    rep = run(sysm, mgr, hubs=HUBS)
    assert rep.ok and rep.mode == "service" and rep.client_id == "ukey-vm-win-1"
    assert mgr.known_hubs == ["10.70.1.208"] and mgr.autofind is False
    calls = len(mgr.calls)
    rep2 = run(sysm, mgr, hubs=HUBS)
    assert rep2.ok and len(mgr.calls) == calls  # 第二次不再有任何变更
    assert any("保留已有" in s.detail for s in rep2.steps)


def test_existing_client_id_is_kept():
    mgr = MockManager(client_id="my-custom-id")
    rep = run(MockSystem("service"), mgr)
    assert rep.client_id == "my-custom-id" and ("set_client_id", "my-custom-id") not in mgr.calls


def test_desired_client_id_overrides_pattern():
    mgr = MockManager()
    run(MockSystem("service"), mgr, desired_client_id="ukey-special")
    assert mgr.client_id == "ukey-special"


def test_standalone_needs_confirmation_when_running_or_custom_config():
    sysm = MockSystem("standalone", standalone_running=True)
    rep = run(sysm, MockManager())
    assert not rep.ok and rep.needs_confirmation and sysm.state == "standalone"
    assert ("switch",) not in sysm.log  # 没确认，绝不切换
    rep = run(MockSystem("standalone", custom_config=True), MockManager())
    assert rep.needs_confirmation


def test_standalone_switches_with_yes_or_when_idle():
    for sysm, yes in [(MockSystem("standalone"), False), (MockSystem("standalone", standalone_running=True), True)]:
        rep = run(sysm, MockManager(), assume_yes=yes)
        assert rep.ok and sysm.state == "service" and ("switch",) in sysm.log


def test_switch_failure_stops_immediately():
    sysm = MockSystem("standalone", switch_rc=5)
    rep = run(sysm, MockManager(), hubs=HUBS)
    assert not rep.ok and names(rep)[-1] == ("切换服务模式", False)
    assert not any(n == "连接 Hub" for n, _ in names(rep))  # 失败即停


def test_missing_manager_installs_after_hash_check(tmp_path):
    f = tmp_path / "inst.exe"
    f.write_bytes(b"official-installer")
    sha = hashlib.sha256(b"official-installer").hexdigest()
    sysm = MockSystem("missing")
    rep = run(sysm, MockManager(), installer_path=str(f), expected_sha256=sha.upper(), hubs=HUBS)
    assert rep.ok and sysm.state == "service"
    order = [n for n, _ in names(rep)]
    assert order.index("校验安装包") < order.index("静默安装") < order.index("切换服务模式")


def test_wrong_hash_never_runs_installer(tmp_path):
    f = tmp_path / "inst.exe"
    f.write_bytes(b"tampered")
    sysm = MockSystem("missing")
    rep = run(sysm, MockManager(), installer_path=str(f), expected_sha256="0" * 64)
    assert not rep.ok and not any(e[0] == "install" for e in sysm.log) and sysm.state == "missing"


def test_missing_hash_refuses_to_install(tmp_path):
    f = tmp_path / "inst.exe"
    f.write_bytes(b"x")
    sysm = MockSystem("missing")
    rep = run(sysm, MockManager(), installer_path=str(f))
    assert not rep.ok and not any(e[0] == "install" for e in sysm.log)


def test_missing_without_installer_source_fails_and_never_downloads_elsewhere():
    sysm = MockSystem("missing")
    rep = run(sysm, MockManager())
    assert not rep.ok and sysm.log == []


def test_download_comes_only_from_given_server_url():
    class Sys(MockSystem):
        def download(self, url, dest, ca_cert=None):
            super().download(url, dest, ca_cert)

    sysm = Sys("missing")
    sha = hashlib.sha256(b"fake-installer").hexdigest()
    rep = run(sysm, MockManager(), installer_url="https://10.70.1.63:8443/agent/manager-installer", expected_sha256=sha)
    assert rep.ok and sysm.log[0] == ("download", "https://10.70.1.63:8443/agent/manager-installer")


def test_installer_failure_stops():
    sysm = MockSystem("missing", installer_rc=1603)
    sha = hashlib.sha256(b"fake-installer").hexdigest()
    rep = run(sysm, MockManager(), installer_url="https://x/agent/manager-installer", expected_sha256=sha)
    assert not rep.ok and names(rep)[-1] == ("静默安装", False) and "1603" in rep.steps[-1].detail


def test_hub_not_visible_is_only_a_hint():
    mgr = MockManager(client_id="c")  # Hub 添加后但没有授权 → mock 的 info 仍列出 Hub，这里改成不可见
    mgr.info = lambda: type(MockManager.info(mgr))(running=True, client_id="c", as_service=True, autofind=False, hubs=[])
    rep = run(MockSystem("service"), mgr, hubs=HUBS)
    assert rep.ok and rep.hub_visible is False
    assert "暂未看到" in rep.steps[-1].detail


def test_apply_hubs_uses_port_suffix_only_when_non_default():
    mgr = MockManager()
    apply_hubs(mgr, [{"address": "10.1.1.1", "port": 18574}, {"address": "10.1.1.2", "port": 19000}])
    assert ("known_hub_add", "10.1.1.1") in mgr.calls and ("known_hub_add", "10.1.1.2:19000") in mgr.calls
