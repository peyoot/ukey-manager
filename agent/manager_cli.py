"""封装 awusbmanager.exe 的调用与 `LIST FULL` 输出解析。

- 一律用参数列表调用 subprocess（不经 shell 拼接）；分组/Hub 地址先按白名单正则校验。
- 命令格式以实测为准（docs/manager-cli-notes.md）：成功输出 `OK`、退出码 0；失败输出 `FAILED`、退出码 1，无原因文本。
- `LIST FULL` 靠缩进区分层级：Hub(1 格) / Hub 字段(2) / 分组(4) / 分组字段(6) / 设备(8) / 设备字段(10)。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

ADDR_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_EXE = r"C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe"


@dataclass
class CliResult:
    rc: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and self.out.strip().upper().startswith("OK")


@dataclass
class Device:
    name: str
    address: str
    vendor: str = ""
    product: str = ""
    serial: str = ""
    port_on_hub: int | None = None
    in_use_by: str = ""
    in_use_by_you: bool = False


@dataclass
class Group:
    name: str
    address: str
    number: int | None = None
    ports: list[int] = field(default_factory=list)
    in_use_by: str = ""
    in_use_by_you: bool = False
    devices: list[Device] = field(default_factory=list)


@dataclass
class Hub:
    address: str            # 如 10.70.1.208:18574
    serial: str = ""
    model: str = ""
    firmware: str = ""
    state: str = ""
    groups: list[Group] = field(default_factory=list)


@dataclass
class ListInfo:
    running: bool = True            # False：Manager 没有运行（`ERROR: AnywhereUSB Manager is not running.`）
    version: str = ""
    client_id: str = ""
    as_service: bool = False
    autofind: bool | None = None
    hubs: list[Hub] = field(default_factory=list)
    raw: str = ""

    def connected_groups(self) -> list[str]:
        """本机（本 Client ID）已连接的分组地址，用于 HEARTBEAT。"""
        return [g.address for h in self.hubs for g in h.groups if g.in_use_by_you]


_HEAD = re.compile(r"^(?P<name>.+?) \((?P<addr>[^()]+)\)(?P<rest>(?: \([^()]*\))*)\s*$")
_FIELD = re.compile(r"^(?P<k>[A-Z][A-Z ]*?):\s*(?P<v>.*)$")


def parse_list(text: str) -> ListInfo:
    info = ListInfo(raw=text)
    if "is not running" in text and "ERROR" in text:
        info.running = False
        return info
    m = re.search(r"AnywhereUSB Manager version (\S+)\s+Client ID:[ \t]*(\S*)", text)
    if m:
        info.version, info.client_id = m.group(1), m.group(2)
    info.as_service = "running as a service" in text
    m = re.search(r"Autofind:\s*(enabled|disabled)", text)
    if m:
        info.autofind = m.group(1) == "enabled"
    hub: Hub | None = None
    group: Group | None = None
    device: Device | None = None
    for line in text.splitlines():
        if not line.strip() or line.startswith(("Below are", "*", "Autofind", "Autoconnect", "AnywhereUSB Manager is")):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        fm = _FIELD.match(body)
        if fm and fm.group("k") in _KNOWN_KEYS:
            k, v = fm.group("k"), fm.group("v").strip()
            if indent == 10 and device:
                _set_device(device, k, v)
            elif indent == 6 and group:
                _set_group(group, k, v)
            elif indent == 2 and hub:
                _set_hub(hub, k, v)
            continue
        hm = _HEAD.match(body)
        if not hm:
            continue
        name, addr, rest = hm.group("name"), hm.group("addr"), hm.group("rest")
        if indent == 1:
            hub = Hub(address=addr)
            info.hubs.append(hub)
            group = device = None
        elif indent == 4 and hub is not None:
            group = Group(name=name, address=addr, in_use_by_you="in-use by you" in rest.lower())
            hub.groups.append(group)
            device = None
        elif indent == 8 and group is not None:
            device = Device(name=name, address=addr, in_use_by_you="in-use by you" in rest.lower())
            group.devices.append(device)
    return info


_KNOWN_KEYS = {
    "NAME", "MODEL", "VERSION", "STATE", "ADDRESS", "PORT", "CONNECTED FOR", "CONNECTION ID", "INTERFACE",
    "SERIAL NUMBER", "AUTOCONNECT", "GROUP", "PORTS", "IN USE BY", "VENDOR", "VENDOR ID", "PRODUCT",
    "PRODUCT ID", "SERIAL", "PORT ON HUB", "ASSIGNED VIRTUAL PORT", "SPEED",
}


def _set_hub(h: Hub, k: str, v: str) -> None:
    if k == "SERIAL NUMBER":
        h.serial = v
    elif k == "MODEL":
        h.model = v
    elif k == "VERSION":
        h.firmware = v.split(" ")[0]
    elif k == "STATE":
        h.state = v


def _set_group(g: Group, k: str, v: str) -> None:
    if k == "GROUP" and v.isdigit():
        g.number = int(v)
    elif k == "PORTS":
        g.ports = [int(x) for x in re.findall(r"\d+", v)]
    elif k == "IN USE BY":
        g.in_use_by = v
        g.in_use_by_you = v.upper() == "YOU"


def _set_device(d: Device, k: str, v: str) -> None:
    if k == "VENDOR":
        d.vendor = v
    elif k == "PRODUCT":
        d.product = v
    elif k == "SERIAL":
        d.serial = v
    elif k == "PORT ON HUB" and v.isdigit():
        d.port_on_hub = int(v)
    elif k == "IN USE BY":
        d.in_use_by = v
        d.in_use_by_you = v.upper() == "YOU"


Runner = Callable[..., "subprocess.CompletedProcess"]


class ManagerCli:
    """真实 Manager。runner 可注入（测试用）。"""

    def __init__(self, exe_path: str = DEFAULT_EXE, *, runner: Runner = subprocess.run, timeout: float = 30):
        self.exe_path, self.runner, self.timeout = exe_path, runner, timeout

    def run(self, *args: str) -> CliResult:
        try:
            p = self.runner(
                [self.exe_path, *args], capture_output=True, timeout=self.timeout, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return CliResult(127, "", "awusbmanager.exe not found")
        except subprocess.TimeoutExpired:
            return CliResult(124, "", "timeout")
        return CliResult(p.returncode, p.stdout or "", p.stderr or "")

    @staticmethod
    def _check(value: str, rx: re.Pattern, what: str) -> str:
        if not rx.match(value):
            raise ValueError(f"非法的 {what}: {value!r}")
        return value

    def info(self) -> ListInfo:
        r = self.run("LIST", "FULL")
        return parse_list(r.out)

    def connect_group(self, address: str) -> CliResult:
        return self.run("CONNECT", f"GROUP,{self._check(address, ADDR_RE, '分组地址')}")

    def disconnect_group(self, address: str) -> CliResult:
        return self.run("DISCONNECT", f"GROUP,{self._check(address, ADDR_RE, '分组地址')}")

    def known_hub_add(self, address: str) -> CliResult:
        return self.run("KNOWN", "HUB", f"ADD,{self._check(address, ADDR_RE, 'Hub 地址')}")

    def known_hub_list(self) -> list[str]:
        r = self.run("KNOWN", "HUB", "LIST")
        return [ln.strip() for ln in r.out.splitlines() if ln.strip() and not ln.startswith("ERROR")]

    def refresh_hubs(self) -> CliResult:
        """让 Manager 立即重新读取 Hub 的分组授权（授权刚变更后，Manager 可能还没看到新分组）。"""
        return self.run("REFRESH", "HUBS")

    def autofind_off(self) -> CliResult:
        return self.run("AUTOFIND,OFF")

    def set_client_id(self, client_id: str) -> CliResult:
        return self.run("SET", f"CLIENTID,{self._check(client_id, CLIENT_ID_RE, 'Client ID')}")
