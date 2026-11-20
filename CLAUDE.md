# CLAUDE.md — AnywhereUSB Plus UKey 管理服务器演示例程

本文件供 Claude Code 使用。目标：在内网 Ubuntu 服务器上实现一个**演示级** UKey 管理服务器及配套的 Windows 客户端代理，配合 Digi AnywhereUSB Plus，演示"端口空闲即分配、用完回收、异常兜底"的基本功能。

**分两步走：**
- **第一步（当前）：1 台 AnywhereUSB Plus（hub-a）+ 1 台 Windows 10 虚拟机（vm-win-1）**，逐项测试 Hub REST API、Manager CLI 和基本功能。
- **第二步（后续）：增加更多 Hub 与虚拟机**，通过控制台向导或 `config.yaml` 添加，不改代码。

因此从第一天起，**Hub 和代理的数量都由配置决定，代码中不得写死 Hub 或虚拟机的个数、名称和 IP**（真实地址只出现在 `config.yaml` 中）。

> 这是演示例程，不是生产系统。优先做到：能跑、能看、能讲清楚架构。所有 AI 生成的代码在对外展示前需由 Digi 工程师审核。

---

## 1. 实验环境

| 角色 | 名称 | 地址 | 说明 |
|---|---|---|---|
| 管理服务器 | ubuntu-srv | **10.70.1.63** | Ubuntu 24.04，Claude Code 在此开发和运行服务器 |
| Hub | hub-a | **10.70.1.208** | AnywhereUSB Plus **AW08**（8 口），序列号 **AW08-017363** |
| 客户端 | vm-win-1 | **10.70.1.92** | Windows 10 虚拟机；同时兼作 Windows 构建机（见第 4 节） |
| 演示用 UKey / U 盘 | — | 插在 hub-a 端口 1、2 | 任意 USB 存储设备也可用于演示；实际端口以用户告知为准 |
| （后续）Hub | hub-b … | | 用控制台"添加 Hub"向导添加 |
| （后续）客户端 | vm-win-2 … | | 用代理安装脚本添加 |

已提供的文件（用户已放在仓库根目录）：

| 文件 | 说明 |
|---|---|
| `config.yaml` | 已按上表填好的服务器配置。**唯一记录真实地址的地方** |
| `.env.example` | 密钥模板。用户复制为 `.env` 并填写；`.env` 绝不提交 |
| `.gitignore` | 已排除 `.env`、`data/`、`backups/`、`certs/`、`dist/` 等 |
| `requirements-server.txt` | 服务器依赖 |
| `agent/requirements.txt` | 代理依赖（Windows 构建机使用） |
| `agent/agent.yaml.example` | 代理配置模板（安装脚本据此生成） |

规则：
- Hub 管理员密码、控制台管理员密码、代理注册令牌只放在 `.env`，**不得**出现在代码、日志、提交记录或控制台页面中。
- Claude Code 只能在 Ubuntu 上运行命令。需要在 vm-win-1 上执行的命令，写成清晰的步骤交给用户，并请用户把输出贴回。远程安装功能（阶段 4.3 方式 B）完成后，可经 WinRM / SSH 在 VM 上执行命令，但每次执行前都要告诉用户将运行什么。
- vm-win-1 兼作构建机和测试客户端：涉及 Manager 安装、模式切换的测试前，先请用户给 VM 打快照，测试后可回滚。

---

## 2. 架构（与演示 PPT 一致）

```
           浏览器（演示控制台 https://10.70.1.63:8443）
                  │ HTTPS
        ┌─────────▼──────────┐   Hub 本地 REST API (HTTPS, /cgi-bin/config.cgi)
        │  管理服务器          │──────────────────────────────► hub-a 10.70.1.208
        │  10.70.1.63         │   GET 读配置 / POST 授权 / DELETE 回收     （后续 hub-b …）
        └─────────▲──────────┘
                  │ WebSocket 长连接（代理主动外连 wss://10.70.1.63:8443/ws/agent，双向）
       ┌──────────┴──────────┐
   vm-win-1 代理 10.70.1.92   （后续 vm-win-2 …）
   └ 调用 awusbmanager.exe（本机 AnywhereUSB Manager CLI，服务模式）
          ⇅ 加密 USB over IP ⇅  hub 上的 UKey
```

两条控制通道：
1. **服务器 → Hub**：通过 Hub 本地 REST API 配置分组、客户端列表，修改"哪个 Client ID 能访问哪个分组"。
2. **服务器 ⇄ 代理**：代理主动建立 WebSocket，服务器经此下发 CONNECT / DISCONNECT；代理在本机调用 Manager CLI 执行。

调度单元：**每个 USB 口 = 一个分组**（分组名 `hub-a-p01` … `hub-a-p08`）。AnywhereUSB Plus 的分组同一时刻只允许一个客户端连接。

扩展性约定（第一步就要遵守）：
- 端口全局 ID = `<hub 名称>-p<两位端口号>`，数据库、协议、控制台都用它。
- 服务器为配置中的**每台** Hub 创建一个 `HubAdapter` 实例；健康检查、对账按列表循环。某台 Hub 离线只影响它自己的端口（标记 fault）。
- 配置变化（向导添加 Hub、手工编辑 config.yaml）后支持热加载：新增 Hub 自动建立端口记录；已移除的 Hub 只告警，不删除历史数据。

---

## 3. 技术栈与目录

- 服务器：Ubuntu 24.04 自带 Python 3.12，使用 venv（系统 Python 受 PEP 668 限制，不要 `pip install --break-system-packages`）。FastAPI、uvicorn、httpx、SQLite、pydantic、ruamel.yaml（**改写 config.yaml 时保留注释**）、python-dotenv。
- 控制台：服务器渲染的静态页面 + 原生 JS（或 HTMX），WebSocket 实时刷新。不引入前端构建工具。
- 代理：Python 3.12，`websockets`、`httpx`、`pyyaml`、`pywin32`；在 Windows 构建机上用 PyInstaller 打包为单个 `agent.exe`。**客户端 VM 不需要安装 Python。**
- 测试：pytest；Hub 和 Manager 均有 mock 实现，单元测试不访问真实设备。

```
server/
  app.py            # FastAPI 入口：REST + WebSocket + 静态页面 + 管理员登录
  config.py         # 读取/热加载 config.yaml 与 .env；向导写回时先备份
  db.py             # SQLite：ports / leases / agents / audit_log
  scheduler.py      # 租约、分配、释放、超时回收
  wizard.py         # "添加 / 编辑 Hub"向导后端（探测、规划、应用、恢复）
  hub/
    base.py         # HubAdapter 抽象接口
    rest.py         # 真实 Hub：AnywhereUSB Plus 本地 REST API
    mock.py         # 测试用
  static/           # 控制台页面：端口总览、代理、Hub 向导、审计日志
agent/
  agent.py          # Windows 客户端代理（主程序，作为 Windows 服务运行）
  manager_cli.py    # 封装 awusbmanager.exe 调用与 LIST 输出解析
  manager_setup.py  # 检测 / 安装 / 切换服务模式 / 设置 Client ID
  winservice.py     # 代理自身注册为 Windows 服务（pywin32）
  manager_mock.py   # Linux 上开发测试用
  build.ps1         # Windows 构建机上打包 agent.exe
  requirements.txt
  agent.yaml.example
  installer/
    install-agent.ps1     # 客户端一键安装脚本（手动或远程调用）
    uninstall-agent.ps1
deploy/
  remote_install.py # 从 Ubuntu 经 WinRM（或 SSH）远程执行 install-agent.ps1
scripts/
  gen_cert.sh       # 生成服务器自签证书（SAN 含 10.70.1.63）
  hub_probe.py      # 阶段 0 / 0.5 探测与逐项测试
common/
  protocol.py       # 消息定义（pydantic 模型）
docs/
  hub-api-notes.md  manager-cli-notes.md  manager-install-notes.md
  api-test-report.md  demo-script.md
dist/     backups/     data/     certs/     # 均不提交
tests/
```

---

## 4. Windows 构建机准备（vm-win-1，由用户手动操作）

`agent.exe` 只能在 Windows 上构建（PyInstaller 不能跨平台打包）。只有构建机需要 Python；其他客户端 VM 只运行打包好的 `agent.exe`。Claude Code 生成 `agent/build.ps1` 和 README 时须与本节一致。

1. **先打快照**（建议命名 `S0-clean`），出问题可回滚。
2. **安装 Python 3.12（64 位）**：从 python.org 下载 Windows installer (64-bit)；勾选 **Add python.exe to PATH**；选 **Customize installation** → 勾选 **Install Python for all users**（装到 `C:\Program Files\Python312`）和 **py launcher**；完成页点击 **Disable path length limit**（若出现）。
3. **验证**（新开 PowerShell）：`py --version`、`python --version`、`pip --version`。若 `python` 弹出 Microsoft Store：设置 → 应用 → 应用执行别名，关闭 python.exe / python3.exe。
4. **允许运行本地脚本**：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
5. **建项目目录与虚拟环境**：
   ```powershell
   mkdir C:\dev\ukey-agent; cd C:\dev\ukey-agent
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r agent\requirements.txt
   ```
6. **无法访问 PyPI 时**：配置内网镜像 `pip config set global.index-url <内网镜像>`；或在可上网的 Windows 64 位 + Python 3.12 机器上 `pip download -r agent\requirements.txt -d wheels`，拷来后 `pip install --no-index --find-links wheels -r agent\requirements.txt`。
7. **取得代码**：从 Ubuntu 拷贝 `agent/`、`common/` 到 `C:\dev\ukey-agent`（Windows 10 自带 `scp`：`scp -r <user>@10.70.1.63:~/ukey-manager/agent .`，`common` 同理），或用 Git for Windows 克隆。
8. **构建**：`.\agent\build.ps1` → 产物 `dist\agent.exe`，拷回服务器的 `dist/`（`scp dist\agent.exe <user>@10.70.1.63:~/ukey-manager/dist/`）。build.ps1 构建后打印 SHA-256。
9. **调试源码**：用**管理员** PowerShell 运行 `python agent\agent.py --config agent.yaml`（服务模式 Manager 的 CLI 需管理员权限）。仅当不打包而用源码注册 Windows 服务时，才需执行一次 `python .venv\Scripts\pywin32_postinstall.py -install`。
10. **再打快照**（`S1-build-env`）。之后的 Manager 安装、模式切换测试都基于 S1，测试完可回滚。
11. 提示：PyInstaller 生成的 exe 偶尔被 Windows Defender 误报，演示时可加入排除项；正式交付建议对 exe 做代码签名。

---

## 5. 开发阶段（按顺序执行，每阶段结束向用户汇报再继续）

第一步（hub-a + vm-win-1）完成阶段 0 ~ 5；阶段 6 在新增设备时执行。

### 阶段 0：服务器环境与只读探测（**必须最先完成，不得跳过**）

1. 服务器环境：`sudo apt install -y python3-venv python3-pip`，建 venv，安装 `requirements-server.txt`；检查 `.env` 已由用户填写（只检查变量是否存在，不打印值）。
2. 网络连通：`ping 10.70.1.208`、`curl -k -I https://10.70.1.208/`。
3. Hub 上 AnywhereUSB 相关配置的确切键名在文档中不完整，**不要凭猜测编写写入逻辑**。只读探测（密码从 `.env` 读取，用 `-u admin:$HUB_PASSWORD_HUB_A` 时不要回显命令）：
   ```bash
   curl -k -u admin https://10.70.1.208/cgi-bin/config.cgi/keys/service/anywhereusb -X GET
   curl -k -u admin https://10.70.1.208/cgi-bin/config.cgi/value/service/anywhereusb -X GET
   ```
   逐层用 `keys` 展开 `groups`、`clients`，弄清：分组如何表示、端口如何归属分组、Client ID 条目结构、"分组访问"字段格式、服务是否启用、服务端口（默认 18574）。同时读取设备型号、序列号、固件版本，与 `config.yaml` 核对（应为 AW08 / AW08-017363）。
4. 完整 AnywhereUSB 配置备份到 `backups/hub-a-<时间>.json`。
5. 结论写入 `docs/hub-api-notes.md`：真实路径、字段、示例请求与响应、仍不确定的点。
6. 请用户在 vm-win-1 上运行 `awusbmanager.exe help`（或不带参数）和 `awusbmanager.exe LIST FULL`（若尚未安装 Manager，则此步移到阶段 0.5 安装测试之后），记录到 `docs/manager-cli-notes.md`。
7. **停下来，把 hub-api-notes.md 给用户确认后再进入写操作。**

### 阶段 0.5：基础 API 与功能逐项测试

用独立小脚本逐项验证，结果记录到 `docs/api-test-report.md`（每项：命令、预期、实际、结论）。脚本从 `config.yaml` 读取 Hub 信息，带 `--hub <name>` 参数，日后可对新 Hub 重跑。

Hub REST API（`scripts/hub_probe.py`），写操作只在 `config.yaml` 的 `test_port`（端口 1）上进行：
- [ ] 读取服务配置、分组列表、客户端列表
- [ ] 修改测试端口的分组归属，写后读校验
- [ ] 把 vm-win-1 的 Client ID 加入客户端列表（若 Hub 支持手动添加）
- [ ] 把分组授予 vm-win-1 的 Client ID；VM 上 Manager 是否立即可见、是否需刷新或重启服务
- [ ] 撤销授权；VM 已连接时撤销会发生什么（立即断开 / 保持到断开为止）—— 决定兜底回收的实现方式
- [ ] 错误路径：错误密码、错误键名、Hub 不可达时的返回码与报错

Manager 安装与模式（基于快照 S1，请用户执行）：
- [ ] 静默安装：`<安装包>.exe /S /v/qn`（Digi 知识库给出，属非官方支持方式，可能随版本变化）；确认安装后为 stand-alone 模式、安装路径、退出码
- [ ] 切换为服务模式：`"C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe" /i /q /g`；回退：`awusbmanager.exe /u`
- [ ] 服务内部名称：`Get-Service | Where-Object DisplayName -like "*AnywhereUSB*"`（显示名 "Digi AnywhereUSB Manager service"）
- [ ] 服务模式配置目录 `C:\Windows\System32\config\systemprofile\AppData\Roaming\AnywhereUSB\`；`awusb.ini` 中 `[Settings]` 的 `ClientId=` 写入后重启服务是否生效
- [ ] 判断当前模式：`awusbmanager.exe LIST` 末尾是否显示以服务方式运行；服务是否存在
- [ ] 安装是否弹出驱动签名 / 信任提示（若有，静默安装可能被阻塞，记录处理办法）

Manager CLI（服务模式下，请用户执行）：
- [ ] `LIST` / `LIST FULL` 输出格式（用于代理解析）
- [ ] `KNOWN HUB ADD,AW08-017363`、`AUTOFIND,OFF`
- [ ] `CONNECT GROUP` / `DISCONNECT GROUP` 的确切语法与返回（地址形如 `AW08-017363.1`）
- [ ] 分组被占用或未授权时 CONNECT 的报错；服务模式下 CLI 所需权限

结果分别写入 `docs/manager-install-notes.md`、`docs/manager-cli-notes.md`。任何一项与本文件写法不符时，以实测为准并更新本文件。

单 VM 无法真实演示"两个客户端争用"。阶段 2 起用 Ubuntu 上的**模拟代理**（`agent.py --mock-manager`，使用只存在于数据库、不写入 Hub 的虚拟 Client ID）充当第二个客户端验证调度逻辑。

### 阶段 1：Hub 适配器

- `HubAdapter` 接口：`identify()`（型号/序列号/固件/端口数）、`service_status()`、`list_groups()`、`set_port_group()`、`list_clients()`、`add_client()`、`remove_client()`、`grant(client_id, group)`、`revoke(client_id, group)`、`backup()`、`restore()`、`health()`。
- `rest.py` 按阶段 0 的真实路径实现；所有写操作"写后读"校验。
- 首次真实写入只在 `test_port` 上进行；扩展到全部端口前需用户确认。
- 命令行工具 `python -m server.setup_groups --hub <name>`（向导的命令行等价物）：打印变更计划，确认后执行；不带 `--hub` 时只打印计划。

### 阶段 2：服务器核心

- 端口状态表：`port_id, hub, group, label, state(idle|leased|connected|fault), client_id, agent_id, lease_expires_at`。
- 租约：TTL、心跳、超时等参数取自 `config.yaml` 的 `lease` 段。
- 分配：REQUEST → 校验代理已批准 → 端口空闲则 `hub.grant()` → 下发 CONNECT → 等待 ACK/EVENT → connected。
- 占用时：返回 busy，向占用方发 RELEASE_ASK；同意则释放后分给申请方。
- 释放：DISCONNECT → ACK → `hub.revoke()` → idle。
- **兜底**：心跳超时或租约到期无响应 → 服务器直接 `hub.revoke()`，端口 idle，写审计日志。
- 启动与热加载时与每台 Hub 对账：Hub 上存在但数据库无租约的授权，告警（可配置自动回收）。
- 控制台需管理员登录（用户名见 config，密码 `CONSOLE_ADMIN_PASSWORD`），会话 cookie 仅 HTTPS。
- 所有动作写 `audit_log`（时间、操作者、代理、端口、动作、结果）。
- 服务器 TLS：`scripts/gen_cert.sh` 生成自签证书，SAN 含 `IP:10.70.1.63`；README 注明生产需受信证书。

### 阶段 3：通信协议（`common/protocol.py`）

传输：`wss://10.70.1.63:8443/ws/agent`。认证：首次用注册令牌 ENROLL，管理员批准后下发代理专属 token；mTLS 作为可选扩展。

```json
{"id": "uuid", "type": "CONNECT", "ts": 1763452800, "payload": {...}}
```

| 方向 | type | payload 要点 |
|---|---|---|
| 代理→服务器 | ENROLL | hostname、IP、注册令牌；IP 在 `auto_approve_ips` 中则自动批准，否则进入控制台待批准列表 |
| 代理→服务器 | REGISTER | hostname, client_id, ip, agent_version, manager_version, manager_mode（service / standalone / missing） |
| 服务器→代理 | CONFIG | Hub 列表（地址 / 序列号）、心跳周期等；代理据此执行 `KNOWN HUB ADD`、`AUTOFIND,OFF` |
| 代理→服务器 | HEARTBEAT | 本机已连接的分组列表（来自 LIST） |
| 代理→服务器 | REQUEST / RELEASE | port_id 或 ukey label |
| 代理→服务器 | EVENT | connected / disconnected / error + 详情 |
| 服务器→代理 | CONNECT / DISCONNECT | hub_serial, group 地址（如 `AW08-017363.1`） |
| 服务器→代理 | RELEASE_ASK | 申请方信息；代理回复 ACK 时带 accept=true/false |
| 服务器→代理 | NOTIFY / RUN_SETUP | 文本通知 / 要求代理重新执行 setup-manager |
| 双向 | ACK | ref_id, ok, error |

规则：服务器指令必须 ACK，超时与重试次数取自配置；消息 id 去重；代理断线指数退避重连，重连后重新 REGISTER，服务器据 HEARTBEAT 对账。

HTTPS REST（控制台与第三方）：`GET /api/ports`、`POST /api/ports/{id}/request`、`POST /api/ports/{id}/release`、`POST /api/ports/{id}/force-release`、`GET /api/agents`、`POST /api/agents/{id}/approve`、`GET /api/audit`，以及第 5 阶段向导接口。

### 阶段 4：Windows 客户端代理（含安装与 Manager 自动部署）

#### 4.1 代理程序本体
- 单个 `agent.exe`，以 **Windows 服务**运行（LocalSystem，与服务模式 Manager 一致），开机自启。
- 配置 `C:\ProgramData\UKeyAgent\agent.yaml`（模板见 `agent/agent.yaml.example`），ACL 仅 SYSTEM 与 Administrators 可读。
- `manager_cli.py` 用 `subprocess.run([...], capture_output=True, timeout=30)`，**参数列表形式，不经 shell 拼接**。
- 用户交互：`agent.exe ui`（命令行菜单：列出可用 UKey / 申请 / 释放 / 同意让出），经 127.0.0.1 回环接口与代理服务通信。
- 启动自检（见 4.2 第 1 步）随 REGISTER 上报；Manager 缺失或异常时代理仍在线，控制台显示"需修复"。

#### 4.2 Manager 检测与自动部署（`agent.exe setup-manager`，幂等）
1. **检测**：`awusbmanager.exe` 是否存在（默认 `C:\Program Files\Digi\AnywhereUSBManager\`）；Manager 服务是否存在/运行；service 还是 standalone；版本。
2. **未安装** → 从服务器 `/agent/manager-installer` 下载 Digi 官方安装包（或用本地文件），校验 `config.yaml` 中的 SHA-256 后静默安装，再次检测。
3. **stand-alone** → `/i /q /g` 切换为服务模式；若 stand-alone 实例正在运行或已有自定义配置，先提示确认（两种模式配置独立）。
4. **Client ID**：服务模式配置中无 ClientId 时按 `client_id_pattern` 写入（默认 `ukey-<主机名>`），重启 Manager 服务并验证；已有则保留。
5. **连接 Hub**：按 CONFIG 执行 `KNOWN HUB ADD`、`AUTOFIND,OFF`。
6. **验证**：`LIST` 能看到 Hub，上报结果与 Client ID。
7. 失败即停，输出错误与日志路径（`C:\ProgramData\UKeyAgent\logs\`），不做破坏性回滚。

不得：卸载用户已有 Manager、降级 Manager、自动从互联网下载 Manager（安装包由用户从 Digi 官网下载放到服务器 `dist/`）。

#### 4.3 安装方式
**方式 A：手动安装（默认，必须实现）**
1. 用户以管理员身份在 VM 上执行：`irm https://10.70.1.63:8443/agent/install.ps1 | iex`。自签证书环境下，先由用户把服务器证书导入 VM 的"受信任的根证书颁发机构"（README 给出命令），或把 `install-agent.ps1` 拷到本地执行并显式加 `-SkipCertCheck`（打印警告）。
2. `install-agent.ps1`：检查管理员权限 → 下载 `agent.exe` 并校验哈希 → `agent.exe setup-manager` → ENROLL → 安装并启动代理服务。
3. 管理员在控制台批准（或 IP 命中 `auto_approve_ips` 自动批准）→ 代理在线。

**方式 B：远程安装（可选，`remote_install.enabled: true` 时启用）**
- `python -m deploy.remote_install --host 10.70.1.92 --user <管理员>`，密码运行时输入，**不保存、不写日志**。
- 优先 WinRM over HTTPS（5986，pywinrm），也支持 Windows OpenSSH Server；远程执行内容与方式 A 相同。
- 连通或权限检查失败时，给出 VM 侧准备步骤，由用户决定是否执行；Claude Code 不得自行修改 VM 的远程管理或防火墙设置。

**卸载**：`uninstall-agent.ps1` 删除代理服务与 `C:\ProgramData\UKeyAgent\`；默认保留 Manager，加 `-RemoveManager` 才卸载。

#### 4.4 服务器侧配套
- 下载端点 `/agent/install.ps1`、`/agent/agent.exe`、`/agent/manager-installer`（只读，来自 `dist/`）。
- 控制台"代理"页：待批准列表；每台代理的 IP、Manager 版本 / 模式 / Client ID / 自检结果；"重新执行 setup-manager"按钮（RUN_SETUP）。
- 代理批准后，引导管理员把该 Client ID 加入各 Hub 的客户端列表（与向导第 4 步共用逻辑）。

### 阶段 5：控制台（含 Hub 配置向导）与演示脚本

#### 5.1 端口总览
- 按 Hub 分组显示端口卡片（颜色 + 文字：空闲 / 已分配 / 已连接 / 故障），Hub 数量变化时自动适应。
- 在线代理列表、审计日志滚动区、每个端口"强制回收"按钮。

#### 5.2 "添加 / 编辑 Hub"向导（管理员页面）

五步向导，每步可返回上一步；在第 5 步确认之前**不对 Hub 做任何写操作**。

1. **基本信息**：名称（自动建议 `hub-b` 等，校验唯一）、IP 地址、管理员用户名（默认 admin）、密码、是否校验 TLS 证书。
2. **连接与识别（只读）**：测试 HTTPS 与认证；从设备读取型号、序列号、固件版本、端口数并显示；读取 AnywhereUSB 服务是否启用、服务端口、现有分组与客户端；自动备份到 `backups/`。若设备返回的配置结构与 `docs/hub-api-notes.md` 不一致（例如固件版本不同），显示警告并**禁止进入写入步骤**。
3. **分组规划**：默认"每口一组"，按 `name_pattern` 生成分组名；表格中可编辑分组描述和 UKey 显示名；可选"保留现有分组，仅登记"；若服务未启用，可勾选"启用 AnywhereUSB 服务"。下方显示与当前配置的差异（新增 / 修改 / 不变）。
4. **Client ID**：列出已批准代理上报的 Client ID，可勾选加入该 Hub 的客户端列表，也可手动输入；授权模式二选一——**动态分配（推荐）**：只加入客户端列表、不预先授予分组，由调度引擎按需授权；**静态授权**：为每个 Client ID 勾选可访问的分组。显示客户端数量与上限（每台 Hub 255 条）。
5. **确认并应用**：汇总所有变更，需再次确认；逐项写入并写后读校验，页面实时显示进度；任一步失败立即停止，并提供"恢复到第 2 步的备份"按钮。成功后：用 ruamel.yaml 把 Hub 写入 `config.yaml`（先备份旧文件到 `backups/config-<时间>.yaml`），密码写入 `.env`（权限 600，变量名 `HUB_PASSWORD_<名称大写>`），热加载，写审计日志。

其他操作：
- **编辑 Hub**：对已有 Hub 从第 2 步开始重走向导（用于修改分组、增减 Client ID）。
- **移除 Hub**：只从服务器配置中移除，不改动 Hub 上的配置；有活动租约时禁止移除。
- 后端接口：`POST /api/hubs/probe`、`POST /api/hubs/plan`、`POST /api/hubs/apply`、`POST /api/hubs/{name}/restore`、`DELETE /api/hubs/{name}`。
- hub-a 已在 `config.yaml` 中预置；第一步中用"编辑 Hub"向导完成 hub-a 的分组与 Client ID 配置，以此验证向导本身。

#### 5.3 演示脚本 `docs/demo-script.md`（标注每个场景需要的最少设备）
1. 控制台显示 hub-a 8 个端口与 vm-win-1 在线。【1 Hub + 1 VM】
2. 用向导为 hub-a 完成分组和 Client ID 配置。【1 + 1】
3. vm-win-1 申请 hub-a 端口 1 的 UKey → 自动授权并连接，设备管理器出现设备。【1 + 1】
4. 第二个客户端申请同一 UKey → vm-win-1 收到 RELEASE_ASK → 同意 → 转给申请方。【第一步用模拟代理；真实演示需 2 台 VM】
5. 控制台强制回收 → VM 断开，Hub 授权撤销。【1 + 1】
6. 停止 vm-win-1 代理服务（模拟崩溃）→ 心跳超时后服务器经 Hub API 自动回收。【1 + 1】
7. 审计日志可查看以上全部操作。【1 + 1】
8. 用向导添加第二台 Hub；一台 Hub 断电后其端口标记故障，另一台正常。【需 2 台 Hub】

### 阶段 6：扩容（新增设备时执行）

- **新增 Hub**：控制台"添加 Hub"向导（首选），或在 `config.yaml` 手工追加后运行 `scripts/hub_probe.py --hub <name>` 与 `setup_groups --hub <name>`。跑场景 8。
- **新增虚拟机**：方式 A 或 B 安装代理 → 控制台批准 → 用"编辑 Hub"向导或批准后的引导，把其 Client ID 加入各 Hub。用真实第二台 VM 重跑场景 4。
- 扩容应**零代码修改**；如需改代码，说明哪里写死了并一并修正。

---

## 6. 安全与操作边界（必须遵守）

- 对 Hub **只允许**读写 AnywhereUSB 服务相关配置（启用、分组、客户端）；不得修改网络、管理员账号、固件、防火墙，不得重启或恢复出厂设置。确有需要先征得用户同意。
- 任何写操作前先备份，写后读校验；提供 `python -m server.restore <backup>`。
- 不在日志、代码、提交记录、页面中输出密码或 token；向导中的密码输入框提交后不回显。
- 自签证书仅限演示：Hub 侧由 `tls_verify: false` 显式关闭校验，服务器侧用 `scripts/gen_cert.sh`；README 注明生产需受信证书。
- 不确定的设备行为一律先探测或询问用户，并记录到 docs，不要猜。
- Windows 侧：只安装 Digi 官方安装包且先校验哈希；安装 / 切换模式测试前先请用户打快照；远程安装密码只在运行时输入；不修改 VM 的防火墙、远程管理、杀毒或 UAC 设置。
- Manager 静默安装与 `/i /q /g` 属 Digi 知识库给出的非官方支持方式，README 须注明，并保留"手动运行 Digi 安装向导选择 Service 模式"的备用说明。

---

## 7. 常用命令

```bash
# Ubuntu 24.04（10.70.1.63）
sudo apt install -y python3-venv python3-pip openssl
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-server.txt
cp .env.example .env && chmod 600 .env      # 用户填写
bash scripts/gen_cert.sh                     # 生成 certs/server.crt / server.key
pytest -q                                    # 单元测试（全 mock）
uvicorn server.app:app --host 0.0.0.0 --port 8443 \
  --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key
python agent/agent.py --config agent/agent.yaml --mock-manager   # Ubuntu 上模拟代理
```

若 Ubuntu 开启了 ufw，需放行 8443：`sudo ufw allow 8443/tcp`（先告知用户）。

## 8. 完成标准

- 阶段 0.5 各项在 `docs/api-test-report.md` 中均有结论。
- `pytest` 全部通过，mock 测试覆盖 **2 台 Hub + 2 个代理**（证明扩展逻辑）。
- 向导：用"编辑 Hub"完成 hub-a 配置；用 mock Hub 完成"添加 Hub"全流程，包括失败后恢复备份。
- 代理：在快照 S1 上，方式 A 一条命令完成"安装 Manager（服务模式）→ 设置 Client ID → 安装代理服务 → 批准 → 申请并连接 UKey"；重启 VM 后自动恢复在线；已装 stand-alone Manager 时能识别并切换。
- 第一步真实环境完成场景 1、2、3、5、6、7，场景 4 用模拟代理完成；第二步完成全部 8 个场景且无需改代码。
- README 说明部署步骤、已知限制（每台 Hub Client ID 上限 255 条、静默安装非官方支持等）与生产化建议（mTLS、高可用、LDAP/AD 对接）。
