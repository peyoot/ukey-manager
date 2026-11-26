# AnywhereUSB Plus UKey 管理服务器（演示例程）

演示"USB 口空闲即分配、用完回收、异常兜底"的基本功能：一台 Ubuntu 管理服务器 + Windows 客户端代理，配合 **Digi AnywhereUSB Plus**，让多台虚拟机按需共享插在 Hub 上的 UKey / U 盘。

> **这是演示例程，不是生产系统。** 

```
        浏览器（控制台 https://10.70.1.63:8443）
               │ HTTPS + WebSocket(/ws/console)
     ┌─────────▼──────────┐   Hub 本地 REST API (HTTPS /cgi-bin/config.cgi)
     │    管理服务器        │───────────────────────────► hub-a 10.70.1.208
     │    10.70.1.63       │   读配置 / 授权 / 回收          （后续 hub-b …）
     └─────────▲──────────┘
               │ WebSocket 长连接（代理主动外连 wss://…/ws/agent）
     ┌─────────┴──────────┐
     │ vm-win-1 上的代理    │ 调用本机 awusbmanager.exe（服务模式）
     └────────────────────┘     ⇅ 加密 USB over IP ⇅  Hub 上的 UKey
```

调度单元：**每个 USB 口 = 一个 Hub 分组**（端口 N = `groupNN`，描述 `hub-a-p06`）。AnywhereUSB Plus 的分组同一时刻只允许一个客户端连接。

## 功能

- **分配 / 回收**：代理申请 → 服务器经 Hub API 授权 → 下发 CONNECT → 本机 Manager 连接；用完 DISCONNECT 再撤销授权。
- **让出**：端口被占用时向占用方发 RELEASE_ASK，同意则转给申请方。
- **兜底**：代理崩溃（心跳超时）或租约到期无响应 → 服务器直接撤销授权（实测：撤销会立即强制断开设备）。
- **对账**：启动与 Hub 恢复在线时，发现 Hub 上存在而数据库没有租约的授权会告警（可配置自动回收）。
- **多 Hub / 多 VM**：数量完全由配置决定。一台 Hub 离线只影响它自己的端口（标记 fault）。
- **控制台**：端口总览（颜色+文字）、代理（批准、自检、重新 setup-manager）、审计日志、**添加 / 编辑 Hub 向导**（探测→规划→逐项写入并写后读校验→失败可恢复），WebSocket 实时刷新。
- **代理**：单个 `agent.exe`，Windows 服务（LocalSystem）；`setup-manager` 幂等地检测 / 安装 Digi Manager、切服务模式、设置 Client ID；`agent.exe ui` 命令行菜单。

## 快速开始（服务器，Ubuntu 24.04）

```bash
sudo apt install -y python3-venv python3-pip openssl
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-server.txt

cp .env.example .env && chmod 600 .env      # 填写 4 个变量（见下）
bash scripts/gen_cert.sh                     # 生成自签证书（SAN 含 10.70.1.63），并打印 SHA-256 指纹
$EDITOR config.yaml                          # Hub 地址、序列号、端口数等（真实地址只写在这里）
pytest -q                                    # 全 mock，不访问真实设备
python -m server                             # HTTPS 8443
```

`.env` 里的变量（**只放这里，绝不提交**）：

| 变量 | 说明 |
|---|---|
| `HUB_PASSWORD_HUB_A` | hub-a 管理员密码（向导添加 Hub 时自动追加 `HUB_PASSWORD_<名称>`） |
| `CONSOLE_ADMIN_PASSWORD` | 控制台管理员密码（用户名见 `config.yaml`） |
| `AGENT_ENROLLMENT_TOKEN` | 代理首次登记令牌：`openssl rand -hex 32` |
| `SESSION_SECRET` | 会话签名密钥：`openssl rand -hex 32` |

若开了 ufw，需放行 8443：`sudo ufw allow 8443/tcp`。浏览器打开 `https://10.70.1.63:8443`，登录后进入控制台（自签证书，浏览器会提示，**生产必须换受信证书**）。

### 第一次配置 Hub

- 首选：控制台「Hub 管理 → 编辑」向导（hub-a 已在 `config.yaml` 预置）。
- 命令行等价物：`python -m server.setup_groups --hub hub-a`（只打印计划；`--apply` 才写，默认只写 `test_port`，`--all-ports` 写全部）。
- 任何写操作前自动备份到 `backups/`；恢复：`python -m server.restore backups/<文件>`（默认只打印计划，`--apply` 才执行）。
- 对 Hub **只**读写 AnywhereUSB 服务相关配置（启用、分组、客户端）；不碰网络、管理员账号、固件、防火墙，也不重启 Hub。

## 部署 Windows 客户端代理

`agent.exe` 只能在 Windows 上用 PyInstaller 构建（**只有构建机需要 Python**，其他 VM 不需要）。完整的构建、安装、验证步骤见 **[docs/agent-windows-steps.md](docs/agent-windows-steps.md)**，概要：

1. 构建机：装 Python 3.12，建虚拟环境，`pip install -r agent\requirements.txt`，运行 `.\agent\build.ps1`（会校验依赖、构建后自检 `agent.exe version`），把 `dist\agent.exe` 拷到服务器 `dist/`。
2. 把 Digi 官方 Manager 安装包放到服务器 `dist/`，在 `config.yaml` 的 `manager_installer` 填文件名和 SHA-256（**服务器只提供哈希与配置一致的安装包**；代理不会从互联网下载 Manager）。
3. **方式 A（默认）**，在 VM 的管理员 PowerShell 里：

   ```powershell
   .\install-agent.ps1 -ServerUrl https://10.70.1.63:8443 -EnrollmentToken "<令牌>" -SkipCertCheck
   ```

   `-SkipCertCheck` 首次信任并**固定**服务器证书，并打印指纹，请与 `gen_cert.sh` 输出核对；也可用 `-ServerCert <server.crt>` 直接指定。脚本依次：校验 `agent.exe` 哈希 → 写 `C:\ProgramData\UKeyAgent\agent.yaml`（仅 SYSTEM / Administrators） → `setup-manager` → 登记 → 创建并启动服务 `UKeyAgent`（开机自启、崩溃自动重启）。
4. 控制台「代理」页批准该代理（或把它的 IP 加到 `config.yaml` 的 `agents.auto_approve_ips`）。
5. 升级：重新运行同一条命令即可（停服务、换 exe、保留专属 token，无需重新批准）。卸载：`uninstall-agent.ps1`（默认保留 Manager，`-RemoveManager` 才卸载）。

**方式 B（可选）**：`python -m deploy.remote_install --host <VM IP> --user <管理员>`，经 WinRM over HTTPS 执行同样的安装；需要 `config.yaml` 里 `remote_install.enabled: true`，VM 侧需自行准备 WinRM。密码运行时输入，不保存；连不上时只打印 VM 侧准备步骤，**不会**代你修改 VM 的远程管理 / 防火墙。这条路径尚未在真实环境验证。

在 VM 上使用：`agent.exe ui --config C:\ProgramData\UKeyAgent\agent.yaml`（列出 UKey / 申请 / 释放 / 状态）。

### Manager 的备用安装方式

静默安装 `<安装包>.exe /S /v/qn` 与切换服务模式 `awusbmanager.exe /i /q /g` 来自 Digi 知识库，**属非官方支持的方式，可能随 Manager 版本变化**。若静默安装失败，请改为手动运行 Digi 安装向导并选择 **Service（服务）模式**，然后再运行 `setup-manager`（它会识别已是服务模式并保留已有 Client ID）。注意：Digi 不建议"先装成 stand-alone 再切服务模式"，两种模式的配置互相独立。

## 演示与文档

| 文档 | 内容 |
|---|---|
| [docs/demo-script.md](docs/demo-script.md) | 8 个演示场景，标注最少设备 |
| [docs/e2e-test-report.md](docs/e2e-test-report.md) | **真实环境验证结果**、实测发现的问题与修复、仍未验证的项 |
| [docs/agent-windows-steps.md](docs/agent-windows-steps.md) | Windows 构建 / 安装 / 验证步骤 |
| [docs/protocol.md](docs/protocol.md) | 服务器 ⇄ 代理协议 |
| [docs/hub-api-notes.md](docs/hub-api-notes.md) | Hub 本地 REST API 实测笔记（含 curl / Python 复现命令） |
| [docs/api-test-report.md](docs/api-test-report.md) | 阶段 0.5 逐项测试（含 curl 手动校验命令） |
| [docs/manager-cli-notes.md](docs/manager-cli-notes.md)、[manager-install-notes.md](docs/manager-install-notes.md) | Manager CLI 与安装 / 模式实测 |

没有真实设备想看页面：`PYTHONPATH=. python scripts/dev_console.py --host 0.0.0.0`，全是 mock，登录信息启动时打印。

## 目录

```
server/    app.py（REST + WebSocket + 静态页 + 登录） scheduler.py wizard.py db.py ws.py config.py hub/{base,rest,mock}.py static/
agent/     agent.py manager_cli.py manager_setup.py manager_mock.py winservice.py build.ps1 installer/
common/    protocol.py（pydantic 消息模型）
deploy/    remote_install.py
scripts/   gen_cert.sh hub_probe.py dev_console.py
docs/ tests/（170 个测试，全 mock，不访问真实设备）
dist/ backups/ data/ certs/    # 均不提交
```

## 已知限制

- **每台 Hub 的客户端条目上限 255 条**（向导会校验并显示）。
- **Manager 静默安装与 `/i /q /g` 属非官方支持**（见上）。
- **自签证书**仅限演示；代理靠"固定证书"信任服务器，Hub 侧 `tls_verify: false`。
- **租约是硬 TTL**（默认 10 分钟），没有自动续租：持有者使用超过 TTL 会被强制断开。需要的话应改为"代理在线且设备仍连接时自动续租"。
- **撤销授权会立即强制断开设备**（实测），所以正常释放先 DISCONNECT 再撤销；强制回收 / 兜底可能造成设备被"拔"，使用中的文件可能丢数据。
- 刚授权的分组，本机 Manager 可能要几秒到几十秒才看得到：代理会 `REFRESH HUBS` 并重试（默认 40 秒），申请因此可能比较慢。
- Hub 的 `config.cgi` 读不到型号 / 序列号 / 固件，服务器侧显示的是 `config.yaml` 声明值（标记为未核实），以 Manager `LIST FULL` 为准。
- 模拟代理（`--mock-manager`）申请端口时，调度器仍会在**真实 Hub** 上为它的虚拟 Client ID 授权，演示后需清理。
- 注册令牌泄露后，攻击者可登记"待批准"代理（仍需管理员批准）；ENROLL 无按 IP 限速。
- 面向第三方的 REST 接口目前只支持管理员会话认证，没有独立的 API 凭据。
- 单机 SQLite，无高可用；代理的 `agent.exe` 未做代码签名，可能被 Defender 误报。
- Windows 侧脚本针对 **Windows PowerShell 5.1**：`.ps1` 必须保存为**带 BOM** 的 UTF-8（否则中文被当 GBK 读、解析失败）；`agent.yaml` 由安装脚本以无 BOM 写入（代理读取时也容忍 BOM）；用 `Get-Content` 看代理日志要加 `-Encoding UTF8`。

## 生产化建议

- **TLS**：换受信 CA 证书；代理与服务器之间启用 **mTLS**（每台代理独立客户端证书，替代 / 补充专属 token）；Hub 侧开启证书校验。
- **认证与授权**：控制台对接 **LDAP / AD**（SSO、RBAC、操作者实名）；第三方 REST 使用独立的 API 凭据并限定范围；ENROLL 增加限速与令牌轮换。
- **高可用**：多实例 + 共享数据库（PostgreSQL 替换 SQLite）；调度锁改为分布式锁；Hub 与代理断连重连逻辑已具备，可直接沿用。
- **密钥管理**：密码和令牌放入 Vault / 系统密钥库，而不是 `.env`；审计日志外送、防篡改。
- **运维**：用 systemd 托管服务器进程（下面是未经验证的示例）；监控 Hub 健康、租约数、回收失败；对 `agent.exe` 做代码签名并走正规分发。
- **产品化**：与 Digi 确认 Manager 无人值守安装 / 模式切换的官方支持方式，以及 Hub 是否提供读取设备状态（端口上插了什么、谁占用）的正式 API，替代目前读取 Manager 输出的做法。

```ini
# /etc/systemd/system/ukey-server.service（示例，未验证）
[Service]
User=robin
WorkingDirectory=/home/robin/mygit/ukey-manager
ExecStart=/home/robin/mygit/ukey-manager/.venv/bin/python -m server
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

## 测试

```bash
pytest -q        # 170 个测试：Hub 适配器（含 2 台 Hub）、调度与兜底、协议与 WebSocket、代理 ↔ 服务器端到端（真实 socket）、
                 # Manager 输出解析（用真实输出做夹具）、setup-manager、向导全流程（含失败后恢复）、控制台安全头与实时推送
```


