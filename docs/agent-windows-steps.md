# 代理在 Windows 上的构建、安装与验证步骤（vm-win-1）

> AI 生成草稿，需 Digi 工程师审核。**下面这些 Windows 侧的代码（PyInstaller 打包、`winservice.py`、`install-agent.ps1`、
> `awusbmanager.exe` 参数传递）都还没有在真实 Windows 上运行过**，Ubuntu 上只验证了协议、调度，以及用假 Manager 的端到端。
> 请逐步执行，并把每步输出贴回；遇到问题以实测为准。

## 0. 先打快照（必须）

给 vm-win-1 打快照（构建环境已装好的话命名 `S1-build-env`；若还没有 Python，先按 CLAUDE.md 第 4 节装好再打）。
安装 Manager / 切换模式 / 安装服务的测试都基于这个快照，测完可回滚。

> 注意：vm-win-1 上已经手工装过 Manager 并切成服务模式，`ukey-vm-win-1` 也已登记。想测"从零安装"，请先回滚到没装 Manager 的快照。
> 不回滚的话，`setup-manager` 应该识别出已是服务模式并保留已有 Client ID，这本身也是个有价值的测试。

## 1. 在构建机上构建 agent.exe

```powershell
cd C:\dev\ukey-agent
scp -r <ubuntu用户>@10.70.1.63:~/mygit/ukey-manager/agent .
scp -r <ubuntu用户>@10.70.1.63:~/mygit/ukey-manager/common .
scp <ubuntu用户>@10.70.1.63:~/mygit/ukey-manager/agent_entry.py .
.\.venv\Scripts\Activate.ps1
pip install -r agent\requirements.txt
.\agent\build.ps1
```

预期：打印 `产物: ...\dist\agent.exe`、SHA-256，以及 `0.1.0`。然后拷回服务器：

```powershell
scp dist\agent.exe <ubuntu用户>@10.70.1.63:~/mygit/ukey-manager/dist/
```

## 2. 先验证两件最不确定的事

**2.1 `awusbmanager.exe` 的参数形式。** 代理用参数列表 `["KNOWN","HUB","ADD,10.70.1.208"]` 调用，应与你在 PowerShell 里手敲等价，但需要实测确认。
在构建机的 venv 里：

```powershell
@'
import subprocess
EXE = r"C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe"
def run(*a):
    p = subprocess.run([EXE, *a], capture_output=True, text=True, errors="replace")
    print(a, "->", p.returncode, repr(p.stdout.strip()[:80]))
run("KNOWN", "HUB", "LIST")
run("KNOWN", "HUB", "ADD,10.70.1.208")
run("AUTOFIND,OFF")
run("SET", "CLIENTID,ukey-vm-win-1")
'@ | python -
```

预期都是 `OK` / 0（`KNOWN HUB LIST` 输出 Hub 列表）。若不是，把输出告诉我，我改 `manager_cli.py` 的参数拼法。
想连带测 CONNECT / DISCONNECT，需要 group06 当前已授权给 `ukey-vm-win-1`。

**2.2 `agent.exe` 能运行：**

```powershell
.\dist\agent.exe version
```

## 3. 在服务器上准备

```bash
cd ~/mygit/ukey-manager && source .venv/bin/activate
bash scripts/gen_cert.sh     # 已生成过则不会覆盖
python -m server             # HTTPS 8443
```

若 Ubuntu 开了 ufw，需放行 8443：`sudo ufw allow 8443/tcp`（先告知用户）。

## 4. 在 VM 上安装（方式 A）

管理员 PowerShell。最稳妥的做法是把 `agent/installer/install-agent.ps1` 拷到 VM 本地执行：

```powershell
.\install-agent.ps1 -ServerUrl https://10.70.1.63:8443 -EnrollmentToken "<服务器 .env 的 AGENT_ENROLLMENT_TOKEN>" -SkipCertCheck
```

`-SkipCertCheck` 会首次信任并固定服务器证书（TOFU），并打印证书 SHA-256 指纹。**务必与服务器上 `bash scripts/gen_cert.sh` 输出的指纹核对。**
也可以改用 `-ServerCert <server.crt 路径>` 直接指定服务器证书，这样不需要 TOFU。

脚本会：校验 agent.exe 哈希、写 `C:\ProgramData\UKeyAgent\agent.yaml`（仅 SYSTEM 与 Administrators 可读）、`setup-manager`、`enroll`、创建并启动服务 `UKeyAgent`。
若检测到 stand-alone Manager 正在运行或有自定义配置，脚本会停下并提示，确认后加 `-AssumeYes` 重跑。

## 5. 批准并验证

控制台页面是阶段 5，目前用 REST 批准（在服务器上）：

```bash
set -a; . ./.env; set +a
curl -sk -c /tmp/cj -H 'content-type: application/json' -d "{\"username\":\"admin\",\"password\":\"$CONSOLE_ADMIN_PASSWORD\"}" https://localhost:8443/api/login
curl -sk -b /tmp/cj https://localhost:8443/api/agents | python3 -m json.tool
curl -sk -b /tmp/cj -X POST https://localhost:8443/api/agents/vm-win-1/approve
```

批准后代理自动注册（最多约 15 秒）。在 VM 上：

```powershell
Get-Service UKeyAgent
Get-Content C:\ProgramData\UKeyAgent\logs\agent.log -Tail 20
& "C:\Program Files\UKeyAgent\agent.exe" ui --config C:\ProgramData\UKeyAgent\agent.yaml
```

然后在服务器上申请端口 6：

```bash
curl -sk -b /tmp/cj -H 'content-type: application/json' -d '{"agent_id":"vm-win-1"}' https://localhost:8443/api/ports/hub-a-p06/request
```

预期：VM 出现磁盘 E:。再调 `force-release`：VM 断开，Hub 授权撤销。重启 VM 后服务应自动恢复在线。

## 6. 回滚

快照回滚，或运行 `uninstall-agent.ps1`（默认保留 Manager，加 `-RemoveManager` 才卸载）。

## 7. 已知未验证项（请重点观察）

| 项 | 风险 |
|---|---|
| `agent.exe` 的 PyInstaller 打包 | 隐式导入的 pywin32 模块可能缺失，会报 `ModuleNotFoundError`，贴给我 |
| `service-run` / `winservice.py` | 服务起不来时看 `sc.exe query UKeyAgent` 和 agent.log |
| `awusbmanager.exe` 参数形式 | 见 2.1 |
| 安装脚本的证书固定（TOFU） | Windows PowerShell 5.1 与 7 的行为差异；见第 4 节 |
| Windows 版 `httpx` 对固定证书的验证 | 代理用 `server_ca_cert` 指向 `server.pem`；TLS 报错请贴日志 |
| 控制台字符编码 | Manager 输出在中文系统上可能是 GBK；解析只依赖 ASCII 关键字，并已用 `errors="replace"` |
