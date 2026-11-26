"""Manager 检测与自动部署（`agent.exe setup-manager`，幂等）。

流程（CLAUDE.md 4.2；判断依据来自阶段 0.5 实测）：
 1 检测：exe 是否存在 / 服务 `awusb` 是否存在 → missing | standalone | service
 2 未安装 → 取得 Digi 官方安装包（本地文件或从**自己的服务器**下载）→ 校验 SHA-256 → 静默安装 `/S /v/qn`
 3 stand-alone → 若实例正在运行或有自定义配置，须 --yes 确认 → `/i /q /g` 切服务模式
 4 Client ID 为空 → `SET CLIENTID,<id>`；已有则保留
 5 按服务器 CONFIG：KNOWN HUB ADD、AUTOFIND,OFF
 6 验证：LIST 为服务模式；能看到 Hub 只作提示（Hub 上未登记/授权前本来就看不到）
失败即停，不做破坏性回滚。不得：卸载已有 Manager、降级、自动从互联网下载。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from .manager_cli import DEFAULT_EXE, ManagerCli

SERVICE_NAME = "awusb"  # 实测：服务名 awusb，显示名 "Digi AnywhereUSB Manager"
STANDALONE_CONFIG = r"%APPDATA%\AnywhereUSB\awusb.ini"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SetupReport:
    steps: list[Step] = field(default_factory=list)
    mode: str = "missing"
    version: str = ""
    client_id: str = ""
    hub_visible: bool | None = None
    needs_confirmation: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append(Step(name, ok, detail))
        return ok

    def summary(self) -> dict:
        return {"ok": self.ok, "mode": self.mode, "version": self.version, "client_id": self.client_id,
                "hub_visible": self.hub_visible, "needs_confirmation": self.needs_confirmation,
                "steps": [s.__dict__ for s in self.steps]}


class WindowsSystem:
    """真实 Windows 环境操作；全部用参数列表调用。仅在 Windows 上使用。"""

    def __init__(self, exe_path: str = DEFAULT_EXE, runner=subprocess.run):
        self.exe_path, self.runner = exe_path, runner

    def _run(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return self.runner(list(args), capture_output=True, text=True, timeout=timeout, errors="replace")

    def exe_exists(self) -> bool:
        return os.path.isfile(self.exe_path)

    def service_exists(self) -> bool:
        return self._run("sc.exe", "query", SERVICE_NAME).returncode == 0

    def service_running(self) -> bool:
        r = self._run("sc.exe", "query", SERVICE_NAME)
        return r.returncode == 0 and "RUNNING" in r.stdout

    def start_service(self) -> bool:
        self._run("sc.exe", "start", SERVICE_NAME)
        for _ in range(20):
            if self.service_running():
                return True
            time.sleep(1)
        return False

    def standalone_instance_running(self) -> bool:
        r = self._run("tasklist.exe", "/FI", "IMAGENAME eq awusbmanager.exe", "/NH")
        return "awusbmanager.exe" in r.stdout.lower()

    def has_standalone_config(self) -> bool:
        appdata = os.environ.get("APPDATA")
        return bool(appdata) and os.path.isfile(os.path.join(appdata, "AnywhereUSB", "awusb.ini"))

    def download(self, url: str, dest: str, ca_cert: str | None = None) -> None:
        import httpx

        with httpx.stream("GET", url, verify=ca_cert or True, timeout=120, follow_redirects=False) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)

    def sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def run_installer(self, path: str) -> int:
        return self._run(path, "/S", "/v/qn", timeout=600).returncode

    def switch_to_service(self) -> int:
        return self._run(self.exe_path, "/i", "/q", "/g", timeout=120).returncode


def detect_state(system) -> str:
    if not system.exe_exists():
        return "missing"
    return "service" if system.service_exists() else "standalone"


def hub_address(h: dict) -> str:
    port = h.get("port", 18574)
    return h["address"] if port == 18574 else f"{h['address']}:{port}"


def apply_hubs(cli, hubs: list[dict]) -> list[str]:
    """按服务器 CONFIG 登记 Hub 并关闭自动发现。幂等；返回动作说明。"""
    actions = []
    known = " ".join(cli.known_hub_list())
    for h in hubs:
        addr = hub_address(h)
        if h["address"] not in known:
            r = cli.known_hub_add(addr)
            actions.append(f"KNOWN HUB ADD {addr}: {'OK' if r.ok else 'FAILED'}")
    if cli.info().autofind is not False:
        r = cli.autofind_off()
        actions.append(f"AUTOFIND,OFF: {'OK' if r.ok else 'FAILED'}")
    return actions


def setup_manager(
    system,
    cli: ManagerCli,
    *,
    hostname: str,
    client_id_pattern: str = "ukey-{hostname}",
    desired_client_id: str | None = None,
    hubs: list[dict] | None = None,
    installer_path: str | None = None,
    installer_url: str | None = None,
    expected_sha256: str | None = None,
    ca_cert: str | None = None,
    assume_yes: bool = False,
    log: Callable[[str], None] = lambda m: None,
) -> SetupReport:
    rep = SetupReport()
    state = detect_state(system)
    rep.mode = state
    log(f"检测：Manager 状态 = {state}")
    rep.add("检测", True, state)

    if state == "missing":
        tmp = None
        path = installer_path
        if not path:
            if not installer_url:
                rep.add("取得安装包", False, "Manager 未安装，且未提供本地安装包或服务器地址")
                return rep
            tmp = tempfile.mkdtemp(prefix="ukey-mgr-")
            path = os.path.join(tmp, "manager-installer.exe")
            try:
                system.download(installer_url, path, ca_cert)
            except Exception as e:
                rep.add("取得安装包", False, f"从服务器下载失败: {type(e).__name__}: {e}")
                return rep
        if not expected_sha256:
            rep.add("校验安装包", False, "缺少期望的 SHA-256，拒绝安装未经校验的安装包")
            return rep
        got = system.sha256(path)
        if got.lower() != expected_sha256.lower():
            rep.add("校验安装包", False, f"SHA-256 不符：期望 {expected_sha256[:12]}…，实际 {got[:12]}…")
            return rep
        rep.add("校验安装包", True, got[:12] + "…")
        rc = system.run_installer(path)
        if not rep.add("静默安装", rc == 0, f"退出码 {rc}"):
            return rep
        state = rep.mode = detect_state(system)
        if not rep.add("安装后检测", state != "missing", state):
            return rep

    if state == "standalone":
        busy = system.standalone_instance_running() or system.has_standalone_config()
        if busy and not assume_yes:
            rep.needs_confirmation = True
            rep.add("切换服务模式", False,
                    "stand-alone 实例正在运行或已有自定义配置（两种模式配置互相独立）；确认后加 --yes 重试")
            return rep
        rc = system.switch_to_service()
        if not rep.add("切换服务模式", rc == 0, f"退出码 {rc}"):
            return rep
        state = rep.mode = detect_state(system)
        if not rep.add("切换后检测", state == "service", state):
            return rep

    if not system.service_running():
        if not rep.add("启动服务", system.start_service(), SERVICE_NAME):
            return rep

    info = cli.info()
    if not rep.add("服务模式自检", info.running and info.as_service, f"版本 {info.version}"):
        return rep
    rep.version = info.version

    if not info.client_id:
        cid = desired_client_id or client_id_pattern.format(hostname=hostname.lower())
        r = cli.set_client_id(cid)
        if not rep.add("设置 Client ID", r.ok, cid):
            return rep
        info = cli.info()
        if not rep.add("验证 Client ID", info.client_id == cid, info.client_id):
            return rep
    else:
        rep.add("Client ID", True, f"保留已有 {info.client_id}")
    rep.client_id = info.client_id

    if hubs:
        acts = apply_hubs(cli, hubs)
        if not rep.add("连接 Hub", all("FAILED" not in a for a in acts), "; ".join(acts) or "已是最新"):
            return rep
        info = cli.info()
        serials = {h.get("serial") for h in hubs if h.get("serial")}
        rep.hub_visible = any(h.serial in serials for h in info.hubs) if serials else bool(info.hubs)
        rep.add("Hub 可见性", True,
                "已看到 Hub" if rep.hub_visible else "暂未看到 Hub（Hub 上登记本 Client ID 并授权后才会出现，属正常）")
    else:
        rep.add("连接 Hub", True, "无 Hub 配置，待连上服务器后由 CONFIG 下发")
    return rep
