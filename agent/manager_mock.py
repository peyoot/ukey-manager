"""Linux 上开发/演示用的假 Manager 与假系统环境（`agent.py --mock-manager`）。

与 ManagerCli / WindowsSystem 同接口。`visible` 模拟 Hub 授权：None = 所有分组可见；
给定集合时，只有集合内的分组才能 CONNECT（否则返回 FAILED，与真实行为一致）。
"""
from __future__ import annotations

from .manager_cli import ADDR_RE, CLIENT_ID_RE, CliResult, Group, Hub, ListInfo


class MockManager:
    def __init__(self, client_id: str = "", *, visible: set[str] | None = None, serial: str = "AW08-MOCK"):
        self.client_id = client_id
        self.known_hubs: list[str] = []
        self.autofind = True
        self.connected: set[str] = set()
        self.visible = visible
        self.serial = serial
        self.calls: list[tuple] = []
        self.fail_connect = False

    def _log(self, *a):
        self.calls.append(a)

    def info(self) -> ListInfo:
        groups = [Group(name=a, address=a, in_use_by="YOU", in_use_by_you=True) for a in sorted(self.connected)]
        hubs = [Hub(address=h, serial=self.serial, model="AnywhereUSB 8 Plus", state="Active (secure)",
                    groups=groups if i == 0 else []) for i, h in enumerate(self.known_hubs)]
        return ListInfo(running=True, version="mock", client_id=self.client_id, as_service=True,
                        autofind=self.autofind, hubs=hubs)

    def connect_group(self, address: str) -> CliResult:
        assert ADDR_RE.match(address), address
        self._log("connect", address)
        if self.fail_connect or (self.visible is not None and address not in self.visible):
            return CliResult(1, "FAILED\n")
        self.connected.add(address)
        return CliResult(0, "OK\n")

    def disconnect_group(self, address: str) -> CliResult:
        assert ADDR_RE.match(address), address
        self._log("disconnect", address)
        self.connected.discard(address)
        return CliResult(0, "OK\n")

    def known_hub_add(self, address: str) -> CliResult:
        self._log("known_hub_add", address)
        if address not in self.known_hubs:
            self.known_hubs.append(address)
        return CliResult(0, "OK\n")

    def known_hub_list(self) -> list[str]:
        return list(self.known_hubs)

    def autofind_off(self) -> CliResult:
        self._log("autofind_off")
        self.autofind = False
        return CliResult(0, "OK\n")

    def set_client_id(self, client_id: str) -> CliResult:
        assert CLIENT_ID_RE.match(client_id), client_id
        self._log("set_client_id", client_id)
        self.client_id = client_id
        return CliResult(0, "OK\n")


class MockSystem:
    """模拟 Windows 侧：exe / 服务 / 安装 / 切换模式。"""

    def __init__(self, state: str = "service", *, standalone_running: bool = False, custom_config: bool = False,
                 installer_rc: int = 0, switch_rc: int = 0):
        self.state = state  # missing | standalone | service
        self.standalone_running, self.custom_config = standalone_running, custom_config
        self.installer_rc, self.switch_rc = installer_rc, switch_rc
        self.log: list[tuple] = []

    def exe_exists(self) -> bool:
        return self.state != "missing"

    def service_exists(self) -> bool:
        return self.state == "service"

    def service_running(self) -> bool:
        return self.state == "service"

    def start_service(self) -> bool:
        return self.state == "service"

    def standalone_instance_running(self) -> bool:
        return self.standalone_running

    def has_standalone_config(self) -> bool:
        return self.custom_config

    def download(self, url: str, dest: str, ca_cert=None) -> None:
        self.log.append(("download", url))
        open(dest, "wb").write(b"fake-installer")

    def sha256(self, path: str) -> str:
        import hashlib
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def run_installer(self, path: str) -> int:
        self.log.append(("install", path))
        if self.installer_rc == 0:
            self.state = "standalone"  # 实测：静默安装后是 stand-alone 模式
        return self.installer_rc

    def switch_to_service(self) -> int:
        self.log.append(("switch",))
        if self.switch_rc == 0:
            self.state = "service"
        return self.switch_rc
