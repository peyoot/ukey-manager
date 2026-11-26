import hashlib
import re
from pathlib import Path

import httpx
import pytest

from deploy.remote_install import build_command
from server.app import create_app
from server.config import AppConfig
from server.db import Db
from tests.conftest import FakeLink

CFG = {
    "hubs": [{"name": "hub-a", "address": "x", "ports": 8, "password_env": "X"}],
    "server": {"public_url": "https://10.70.1.63:8443"},
    "manager_installer": {"file": "dist/mgr.exe", "sha256": hashlib.sha256(b"digi-official").hexdigest()},
    "agent_package": {"file": "dist/agent.exe", "sha256": "REPLACE_AFTER_BUILD"},
}


def make(tmp_path, cfg=CFG):
    (tmp_path / "dist").mkdir(exist_ok=True)
    return create_app(AppConfig.model_validate(cfg), {}, FakeLink(), Db(":memory:"), admin_user="a", admin_password="p",
                      session_secret="s" * 32, background=False, base_dir=tmp_path)


async def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_downloads_need_no_login_and_are_read_only(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "mgr.exe").write_bytes(b"digi-official")
    (tmp_path / "dist" / "agent.exe").write_bytes(b"MZ-agent")
    async with await client(make(tmp_path)) as c:
        m = (await c.get("/agent/manifest.json")).json()
        assert m["server_url"] == "wss://10.70.1.63:8443/ws/agent"
        assert m["agent"]["sha256"] == hashlib.sha256(b"MZ-agent").hexdigest()   # 实时计算，不依赖配置里的占位值
        assert m["manager"]["verified"] is True
        assert (await c.get("/agent/agent.exe")).content == b"MZ-agent"
        assert (await c.get("/agent/manager-installer")).content == b"digi-official"
        for method in ("POST", "PUT", "DELETE"):
            assert (await c.request(method, "/agent/agent.exe")).status_code == 405
        assert (await c.get("/agent/../config.yaml")).status_code == 404   # 没有路径参数可穿越


async def test_missing_files_return_404(tmp_path):
    async with await client(make(tmp_path)) as c:
        m = (await c.get("/agent/manifest.json")).json()
        assert m["agent"]["available"] is False and m["manager"]["available"] is False
        assert (await c.get("/agent/agent.exe")).status_code == 404
        assert (await c.get("/agent/manager-installer")).status_code == 404


async def test_tampered_manager_installer_is_refused(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "mgr.exe").write_bytes(b"tampered!")
    async with await client(make(tmp_path)) as c:
        assert (await c.get("/agent/manager-installer")).status_code == 409
        assert (await c.get("/agent/manifest.json")).json()["manager"]["verified"] is False


async def test_install_script_is_served_with_server_url_filled_in(tmp_path):
    async with await client(make(tmp_path)) as c:
        r = await c.get("/agent/install.ps1")
        assert r.status_code == 200 and "https://10.70.1.63:8443" in r.text and "__SERVER_URL__" not in r.text


def test_installer_scripts_follow_the_safety_rules():
    ps = Path("agent/installer/install-agent.ps1").read_text(encoding="utf-8")
    assert "IsInRole" in ps and "Get-FileHash" in ps and "setup-manager" in ps and "New-Service" in ps
    assert "LocalSystem" in ps and "icacls" in ps and "SYSTEM:(OI)(CI)F" in ps
    for forbidden in ("netsh", "Set-NetFirewall", "Disable-", "EnableLUA", "Set-MpPreference", "Enable-PSRemoting", "winrm quickconfig"):
        assert forbidden not in ps, forbidden           # 不改防火墙 / UAC / 杀毒 / 远程管理
    assert "Invoke-Expression" not in ps and "iex " not in ps.replace("irm", "")
    un = Path("agent/installer/uninstall-agent.ps1").read_text(encoding="utf-8")
    assert "-RemoveManager" in un and un.index("if ($RemoveManager)") > un.index("Remove-Item")  # 默认保留 Manager


def test_remote_command_quotes_token_and_does_not_leak_it_elsewhere():
    cmd = build_command("https://10.70.1.63:8443", "to'k;en", ["-SkipCertCheck"])
    assert "-EnrollmentToken 'to''k;en'" in cmd and cmd.count("to''k") == 1
    assert "/agent/install.ps1" in cmd


def test_powershell_scripts_have_utf8_bom():
    """Windows PowerShell 5.1 把无 BOM 的 .ps1 当 ANSI(GBK) 读，中文会乱码并导致解析错误，所以必须带 UTF-8 BOM。"""
    for f in Path("agent").rglob("*.ps1"):
        assert f.read_bytes().startswith(b"\xef\xbb\xbf"), f


async def test_served_install_script_has_no_bom_char(tmp_path):
    async with await client(make(tmp_path)) as c:
        assert not (await c.get("/agent/install.ps1")).text.startswith("\ufeff")


def test_install_script_avoids_known_powershell_5_1_pitfalls():
    ps = Path("agent/installer/install-agent.ps1").read_text(encoding="utf-8-sig")
    assert "GetMethod" not in ps                                  # MethodInfo 不能转成委托（实测报错）
    assert "[UKeyCertPin]::Callback" in ps and "RemoteCertificateValidationCallback Callback" in ps
    assert "New-Service" in ps and "sc.exe create" not in ps      # sc.exe 的 binPath 引号在 5.1 里会被吃掉
    assert "-Encoding utf8" not in ps and "UTF8Encoding $false" in ps   # 5.1 的 utf8 会写 BOM，PyYAML 读不了


def test_agent_config_tolerates_utf8_bom(tmp_path):
    from agent.agent import AgentConfig

    f = tmp_path / "agent.yaml"
    f.write_bytes(b"\xef\xbb\xbfserver_url: wss://x/ws/agent\nenrollment_token: tok\nhostname: VM\n")
    cfg = AgentConfig.load(f)
    assert cfg.server_url == "wss://x/ws/agent" and cfg.enrollment_token == "tok"
    cfg.agent_token = "t2"
    cfg.save_tokens()
    assert "agent_token: t2" in f.read_text(encoding="utf-8")


def test_installer_upgrade_keeps_real_token_but_not_empty_quotes():
    ps = Path("agent/installer/install-agent.ps1").read_text(encoding="utf-8-sig")
    assert """.Trim("'").Trim('"')""" in ps      # 否则 agent_token: '' 会被当成已有 token（''），升级后写出坏配置
    assert "-Quiet" not in ps.split("$keepToken")[1].split("$enrollField")[0]
