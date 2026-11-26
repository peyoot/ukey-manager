"""方式 B：从 Ubuntu 经 WinRM(HTTPS) 在 VM 上执行与方式 A 相同的安装脚本。

  python -m deploy.remote_install --host 10.70.1.92 --user Administrator [--dry-run]

- 仅当 config.yaml 的 remote_install.enabled 为 true 时可用；密码运行时输入，不保存、不写日志。
- 连通 / 权限检查失败时只打印 VM 侧的准备步骤，由用户决定是否执行；本工具绝不修改 VM 的远程管理、防火墙等设置。
- 注册令牌来自 .env（AGENT_ENROLLMENT_TOKEN），仅通过 WinRM HTTPS 传给 VM，不打印。
"""
from __future__ import annotations

import argparse
import getpass
import sys

from dotenv import dotenv_values

from server.config import ROOT, load_config

VM_PREP = """\
VM 侧需要先（由你在管理员 PowerShell 中）准备 WinRM over HTTPS，本工具不会代劳：
  1. 创建自签证书并建 HTTPS 监听（端口 5986），例如：
       $c = New-SelfSignedCertificate -DnsName $env:COMPUTERNAME -CertStoreLocation Cert:\\LocalMachine\\My
       New-Item -Path WSMan:\\localhost\\Listener -Transport HTTPS -Address * -CertificateThumbPrint $c.Thumbprint -Force
  2. 放行入站 TCP 5986（仅允许管理服务器 10.70.1.63），并确认账户有管理员权限。
  3. 在服务器上先用 --dry-run 验证连通。
替代方案：安装 Windows OpenSSH Server，改用 ssh 方式；或直接用方式 A（在 VM 上运行 install.ps1）。"""


def build_command(server_url: str, token: str, extra: list[str]) -> str:
    """远端执行的 PowerShell：下载服务器上的 install.ps1 并执行（令牌作为参数传入）。"""
    q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
    flags = " ".join(extra)
    return (
        "$ErrorActionPreference='Stop';"
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
        "[Net.ServicePointManager]::ServerCertificateValidationCallback={$true};"  # 仅用于取脚本；脚本内部会固定证书
        f"$s=(Invoke-WebRequest {q(server_url.rstrip('/') + '/agent/install.ps1')} -UseBasicParsing).Content;"
        f"& ([scriptblock]::Create($s)) -EnrollmentToken {q(token)} {flags}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--port", type=int)
    ap.add_argument("--dry-run", action="store_true", help="只检查连通与权限，不安装")
    ap.add_argument("--insecure-winrm", action="store_true", help="不校验 WinRM 证书（自签证书时需要；会打印警告）")
    ap.add_argument("--assume-yes", action="store_true", help="允许切换 stand-alone Manager 为服务模式")
    a = ap.parse_args(argv)

    cfg = load_config()
    ri = (cfg.model_extra or {}).get("remote_install", {})
    if not ri.get("enabled"):
        print("remote_install.enabled 为 false：方式 B 未启用。请先在 config.yaml 中开启，或使用方式 A。", file=sys.stderr)
        return 2
    if ri.get("method", "winrm") != "winrm":
        print("目前只实现了 winrm；ssh 方式请手动执行方式 A。", file=sys.stderr)
        return 2
    try:
        import winrm
    except ImportError:
        print("缺少 pywinrm：pip install pywinrm", file=sys.stderr)
        return 2
    password = getpass.getpass(f"{a.user}@{a.host} 的密码（不会保存）: ")
    if a.insecure_winrm:
        print("警告：未校验 WinRM 证书。", file=sys.stderr)
    port = a.port or ri.get("winrm_port", 5986)
    try:
        sess = winrm.Session(f"https://{a.host}:{port}/wsman", auth=(a.user, password), transport="ntlm",
                             server_cert_validation="ignore" if a.insecure_winrm else "validate")
        r = sess.run_ps("[bool]([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
                        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
        is_admin = r.status_code == 0 and r.std_out.strip().lower() == b"true"
    except Exception as e:  # 连不上 / 认证失败 / 证书
        print(f"WinRM 连接失败: {type(e).__name__}\n\n{VM_PREP}", file=sys.stderr)
        return 3
    finally:
        password = ""
    if not is_admin:
        print(f"账户 {a.user} 不是管理员，无法安装。\n\n{VM_PREP}", file=sys.stderr)
        return 3
    print("WinRM 连通，且具有管理员权限。")
    if a.dry_run:
        return 0
    token = dotenv_values(ROOT / ".env").get((cfg.model_extra or {}).get("agents", {}).get("enrollment_token_env", "AGENT_ENROLLMENT_TOKEN"))
    if not token:
        print("AGENT_ENROLLMENT_TOKEN 未设置", file=sys.stderr)
        return 2
    server_url = (cfg.model_extra or {}).get("server", {}).get("public_url")
    print(f"即将在 {a.host} 上执行：下载并运行 {server_url}/agent/install.ps1（-SkipCertCheck 首次信任并固定服务器证书）")
    extra = ["-SkipCertCheck"] + (["-AssumeYes"] if a.assume_yes else [])
    r = sess.run_ps(build_command(server_url, token, extra))
    print(r.std_out.decode("utf-8", "replace"))
    if r.std_err:
        print(r.std_err.decode("utf-8", "replace"), file=sys.stderr)
    return r.status_code


if __name__ == "__main__":
    raise SystemExit(main())
