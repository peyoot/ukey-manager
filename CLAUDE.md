# CLAUDE.md — AnywhereUSB Plus UKey 管理服务器演示例程

本文件供 Claude Code 使用。目标：在内网 Ubuntu 服务器上实现一个**演示级** UKey 管理服务器，配合 Digi AnywhereUSB Plus 和 Windows 虚拟机，演示"端口空闲即分配、用完回收、异常兜底"的基本功能。

**分两步走：**
- **第一步（当前）：1 台 AnywhereUSB Plus + 1 台 Windows 虚拟机**，逐项测试 Hub REST API、Manager CLI 和基本功能。
- **第二步（后续）：增加更多 Hub 与虚拟机**，只改配置、不改代码。

因此从第一天起，**Hub 和代理的数量都由 `config.yaml` 决定，代码中不得写死 Hub 或虚拟机的个数、名称和 IP。**

> 这是演示例程，不是生产系统。优先做到：能跑、能看、能讲清楚架构。所有 AI 生成的代码在对外展示前需由 Digi 工程师审核。

---

## 1. 实验环境（开始前请用户填写，未填写时先询问）

第一步只需填写前三行；后续新增设备时在表中追加行，并同步追加到 `config.yaml`。

| 角色 | 名称 | 地址 | 备注 |
|---|---|---|---|
| 管理服务器 | ubuntu-srv | `<UBUNTU_IP>` | Ubuntu，Claude Code 在此运行 |
| Hub | hub-a | `<HUB_A_IP>` | 型号 `<AW02/AW08/AW24>`，序列号 `<AWxx-xxxxxx>` |
| 客户端 | vm-win-1 | `<VM1_IP>` | Windows，已装 AnywhereUSB Manager，Client ID `<CID1>` |
| 演示用 UKey/U 盘 | — | 插在 `<hub-a 端口 1、2>` | 任意 USB 存储设备也可用于演示 |
| （后续）Hub | hub-b … | | |
| （后续）客户端 | vm-win-2 … | | |

- Hub 管理员密码只放在 `.env`，按 Hub 名称命名变量（`HUB_PASSWORD_HUB_A=...`），**绝不提交到仓库**，`.gitignore` 必须包含 `.env`。
- `config.yaml` 结构示例（列表形式，增删设备只改这里）：
  ```yaml
  hubs:
    - name: hub-a
      address: 192.168.x.x
      serial: AW08-xxxxxx
      ports: 8              # 以探测结果为准
      tls_verify: false     # 演示环境自签证书
  agents:
    - name: vm-win-1
      client_id: <CID1>
      token_env: AGENT_TOKEN_VM_WIN_1
  ukeys:                    # 可选：端口 → UKey 显示名称
    - {hub: hub-a, port: 1, label: "演示 U 盘 1"}
  ```
- Claude Code 只能在 Ubuntu 上运行命令。Windows 客户端代理在 Ubuntu 上开发与测试（用 mock），再由用户手动拷贝到虚拟机运行；需要在虚拟机上执行的命令，写成清晰的步骤交给用户。

---

## 2. 架构（与演示 PPT 一致）

```
           浏览器（演示控制台）
                  │ HTTPS
        ┌─────────▼──────────┐   Hub 本地 REST API (HTTPS, /cgi-bin/config.cgi)
        │  管理服务器 (Ubuntu) │──────────────────────────────► hub-a / hub-b
        │  FastAPI + SQLite   │   GET 读配置 / POST 授权 / DELETE 回收
        └─────────▲──────────┘
                  │ WebSocket 长连接（代理主动外连，双向）
       ┌──────────┴──────────┐
   vm-win-1 代理          vm-win-2 代理
   └ 调用 awusbmanager.exe（本机 AnywhereUSB Manager CLI）
          ⇅ 加密 USB over IP ⇅  hub 上的 UKey
```

两条控制通道：
1. **服务器 → Hub**：通过 Hub 本地 REST API 修改"哪个 Client ID 能访问哪个分组"。
2. **服务器 ⇄ 代理**：代理主动建立 WebSocket，服务器经此下发 CONNECT / DISCONNECT；代理在本机调用 Manager CLI 执行。

调度单元：**每个 USB 口 = 一个分组**（分组名如 `hub-a-p01`）。AnywhereUSB Plus 的分组同一时刻只允许一个客户端连接。

扩展性约定（第一步就要遵守）：
- 端口全局 ID = `<hub 名称>-p<两位端口号>`，数据库、协议、控制台都用它，不依赖 Hub 个数。
- 服务器为 `config.yaml` 中的**每台** Hub 创建一个 `HubAdapter` 实例；健康检查、对账都按列表循环。某台 Hub 离线只影响它自己的端口（标记 fault），不影响其他 Hub。
- 代理按 `agents` 列表认证；新增虚拟机 = 在 Manager 中获取 Client ID → 追加到 `config.yaml` → 部署同一份代理程序。
- 启动时检测 `config.yaml` 与数据库差异：新增的 Hub 自动建立端口记录；已删除的 Hub 只告警，不自动删除历史数据。

---

## 3. 技术栈与目录

- Python 3.11+；服务器：FastAPI、uvicorn、httpx、SQLite（标准库 sqlite3 或 SQLModel）、pydantic、PyYAML。
- 控制台：单页 HTML + 原生 JS（或 HTMX），通过 WebSocket 实时刷新端口状态表。不引入前端构建工具。
- 代理：纯 Python（`websockets` 库），可用 PyInstaller 在 Windows 上打包为 exe；开发阶段直接 `python agent.py`。
- 测试：pytest；Hub 和 Manager 均需有 mock 实现，单元测试不访问真实设备。

```
server/
  app.py            # FastAPI 入口：REST + WebSocket + 静态页面
  config.py         # 读取 config.yaml 与 .env
  db.py             # SQLite：ports / leases / agents / audit_log
  scheduler.py      # 租约、分配、释放、超时回收
  hub/
    base.py         # HubAdapter 抽象接口
    rest.py         # 真实 Hub：AnywhereUSB Plus 本地 REST API
    mock.py         # 测试用
  static/index.html # 演示控制台
agent/
  agent.py          # Windows 客户端代理
  manager_cli.py    # 封装 awusbmanager.exe 调用与 LIST 输出解析
  manager_mock.py   # Linux 上开发测试用
common/
  protocol.py       # 消息定义（pydantic 模型）
docs/
  hub-api-notes.md  # 第 0 阶段探测结果（必须先产出）
  manager-cli-notes.md
  demo-script.md    # 演示步骤
config.example.yaml
.env.example
tests/
```

---

## 4. 开发阶段（按顺序执行，每阶段结束向用户汇报再继续）

第一步（单 Hub + 单 VM）完成阶段 0 ~ 5；阶段 6 在新增设备时执行。

### 阶段 0：只读探测（**必须最先完成，不得跳过**）

Hub 上 AnywhereUSB 相关配置的确切键名在文档中不完整，**不要凭猜测编写写入逻辑**。

1. 用只读请求探测每台 Hub（示例，注意 `-k` 仅用于演示环境的自签证书）：
   ```bash
   curl -k -u admin https://<HUB_IP>/cgi-bin/config.cgi/keys/service/anywhereusb -X GET
   curl -k -u admin https://<HUB_IP>/cgi-bin/config.cgi/value/service/anywhereusb -X GET
   ```
   逐层用 `keys` 展开 `groups`、`clients` 节点，弄清：分组如何表示、端口如何归属分组、Client ID 条目的结构、"分组访问"字段的格式。
2. 把完整的 AnywhereUSB 配置备份到 `backups/<hub>-<时间>.json`。
3. 把结论写入 `docs/hub-api-notes.md`：真实路径、字段、示例请求与响应、仍不确定的点。
4. 请用户在 vm-win-1 上运行 `awusbmanager.exe help`（或 `awusbmanager.exe` 不带参数）和 `awusbmanager.exe LIST FULL`，把输出贴回来；记录到 `docs/manager-cli-notes.md`，确认 `CONNECT GROUP` / `DISCONNECT GROUP` 的确切参数格式和 exe 安装路径。
5. **停下来，把 hub-api-notes.md 给用户确认后再进入写操作。**

### 阶段 0.5：基础 API 与功能逐项测试（单 Hub + 单 VM）

在写服务器之前，先用独立的小脚本把每个基础能力单独验证一遍，结果记录到 `docs/api-test-report.md`（每项：命令、预期、实际、结论）。脚本放在 `scripts/`，全部从 `config.yaml` 读取 Hub 信息，带 `--hub <name>` 参数，以便日后对新 Hub 重跑。

Hub REST API（`scripts/hub_probe.py`）：
- [ ] 读取 AnywhereUSB 服务配置、分组列表、客户端列表
- [ ] 修改一个分组的端口归属（只在测试端口上），写后读校验
- [ ] 把分组授予 vm-win-1 的 Client ID；VM 上 Manager 是否立即可见、是否需刷新或重启服务
- [ ] 撤销授权；VM 已连接时撤销会发生什么（立即断开 / 保持到断开为止）—— 这一点决定兜底回收的实现方式
- [ ] 错误路径：错误密码、错误键名、Hub 不可达时的返回码与报错

Manager CLI（请用户在 vm-win-1 执行，Claude Code 提供逐条命令并记录输出）：
- [ ] `LIST` / `LIST FULL` 输出格式（用于代理解析）
- [ ] `CONNECT GROUP` / `DISCONNECT GROUP` 的确切语法与返回
- [ ] 分组被其他客户端占用、或未授权时 CONNECT 的报错
- [ ] Manager 以服务模式运行时 CLI 是否可用、需要何种权限

单 VM 无法真实演示"两个客户端争用"。阶段 2 起用 Ubuntu 上的**模拟代理**（`agent.py --mock-manager`，使用一个只存在于数据库、不写入 Hub 的虚拟 Client ID）充当第二个客户端，验证调度逻辑；真实争用演示留到新增第二台 VM 之后。

### 阶段 1：Hub 适配器

- `HubAdapter` 接口：`list_ports()`、`list_groups()`、`list_clients()`、`grant(client_id, group)`、`revoke(client_id, group)`、`health()`。
- `rest.py` 按阶段 0 的真实路径实现；对写操作做"写后读"校验。
- 首次真实写入只在用户指定的**一个测试端口**上进行，验证：授权后 VM 能否立即看到/连接该分组，是否需要重启服务才能生效；结论补充到 notes。
- 初始化命令 `python -m server.setup_groups --hub <name>`：把指定 Hub 的每个端口设为独立分组（`hub-a-p01` …），执行前打印变更计划并要求用户确认。不带 `--hub` 时只打印所有 Hub 的计划，不执行。

### 阶段 2：服务器核心

- 端口状态表：`port_id, hub, group, label(UKey 名称), state(idle|leased|connected|fault), client_id, agent_id, lease_expires_at`。
- 租约：默认 TTL 10 分钟（演示可配置为 2 分钟），代理心跳续租。
- 分配流程：REQUEST → 校验 agent 是否允许 → 端口空闲则 `hub.grant()` → 下发 CONNECT → 等待 ACK/EVENT → 状态 connected。
- 端口被占用时：返回 busy，并向占用方发送 RELEASE_ASK；占用方同意则走释放流程后分给申请方。
- 释放：下发 DISCONNECT → ACK → `hub.revoke()` → idle。
- **兜底**：代理心跳超时（默认 45 秒）或租约到期无响应 → 服务器直接 `hub.revoke()`，端口标记 idle 并写审计日志。
- 启动时与 Hub 对账：Hub 上存在但数据库中没有租约的授权，记录告警（演示中可选择自动回收）。
- 所有动作写 `audit_log`（时间、操作者、agent、端口、动作、结果）。

### 阶段 3：通信协议（`common/protocol.py`）

传输：`wss://<UBUNTU_IP>:8443/ws/agent`（演示可先用 ws:// + token，最后再加自签 TLS）。代理认证：演示期每个代理一个预共享 token（config.yaml 中配置）；mTLS 作为可选扩展。

统一消息格式：
```json
{"id": "uuid", "type": "CONNECT", "ts": 1763452800, "payload": {...}}
```

| 方向 | type | payload 要点 |
|---|---|---|
| 代理→服务器 | REGISTER | hostname, client_id, ip, agent_version |
| 代理→服务器 | HEARTBEAT | 本机已连接的分组列表（来自 LIST） |
| 代理→服务器 | REQUEST / RELEASE | port_id 或 ukey_label |
| 代理→服务器 | EVENT | connected / disconnected / error + 详情 |
| 服务器→代理 | CONNECT / DISCONNECT | hub_serial, group 地址（如 `AW08-xxxxxx.1`） |
| 服务器→代理 | RELEASE_ASK | 申请方信息；代理回复 ACK 时带 accept=true/false |
| 服务器→代理 | NOTIFY | 文本通知 |
| 双向 | ACK | ref_id（被确认消息的 id）, ok, error |

规则：每条服务器指令必须收到 ACK，5 秒未收到重试 2 次后告警；消息 id 去重；代理断线指数退避重连，重连后重新 REGISTER，服务器据 HEARTBEAT 对账。

另提供 HTTPS REST（供控制台与第三方）：`GET /api/ports`、`POST /api/ports/{id}/request`、`POST /api/ports/{id}/release`、`GET /api/audit`、`GET /api/agents`。

### 阶段 4：Windows 客户端代理

- 配置文件 `agent.yaml`：server_url、token、awusbmanager 路径、client_id。
- `manager_cli.py` 用 `subprocess.run([...], capture_output=True, timeout=30)` 调用，**参数列表形式，不经 shell 拼接**；按 `docs/manager-cli-notes.md` 解析 LIST 输出。
- 演示期间代理提供一个极简交互（命令行菜单即可：列出可用 UKey / 申请 / 释放 / 同意让出），服务器指令自动执行。
- Manager 需在运行状态（服务模式需管理员权限）；代理启动时检查并给出明确报错。
- 在 Ubuntu 上用 `manager_mock.py` 跑通全流程测试，再交给用户部署到 VM。

### 阶段 5：演示控制台与脚本

- `index.html`：两台 Hub 的端口卡片（颜色+文字表示状态：空闲 / 已分配 / 已连接 / 故障）、在线代理列表、审计日志滚动区、每个端口的"强制回收"按钮。
- 控制台按 Hub 分组显示端口卡片，Hub 数量变化时自动适应（不写死布局）。
- `docs/demo-script.md` 写清以下演示场景及操作步骤（标注每个场景需要的最少设备）：
  1. 控制台显示所有 Hub 的端口与在线代理。【1 Hub + 1 VM】
  2. vm-win-1 申请 hub-a 端口 1 的 UKey → 自动授权并连接，VM 设备管理器中出现设备。【1 + 1】
  3. 第二个客户端申请同一 UKey → 提示占用，vm-win-1 收到 RELEASE_ASK → 同意 → UKey 转给申请方。【第一步用模拟代理验证逻辑；真实演示需 2 台 VM】
  4. 管理员在控制台强制回收 → VM 断开，Hub 授权被撤销。【1 + 1】
  5. 关掉 vm-win-1 的代理（模拟崩溃）→ 45 秒后服务器经 Hub API 自动回收端口。【1 + 1】
  6. 审计日志可查看以上全部操作。【1 + 1】
  7. 一台 Hub 断电或断网 → 其端口标记故障，其他 Hub 正常使用。【需 2 台 Hub】

### 阶段 6：扩容（新增 Hub / 虚拟机时执行）

新增 Hub：
1. 在第 1 节表格和 `config.yaml` 中追加，密码写入 `.env`。
2. 对新 Hub 运行 `scripts/hub_probe.py --hub <name>`（只读），确认固件版本与键名和 `docs/hub-api-notes.md` 一致；不一致时先更新 notes 再改适配器。
3. 备份新 Hub 配置，运行 `setup_groups --hub <name>`。
4. 重启服务器，确认控制台出现新 Hub 端口；跑场景 7。

新增虚拟机：
1. 在新 VM 上安装 AnywhereUSB Manager，获取 Client ID；把该 Client ID 加入各 Hub 的客户端列表（按 hub-api-notes 中验证过的方式）。
2. 在 `config.yaml` 追加代理及 token，部署同一份代理程序。
3. 用真实的第二台 VM 重跑场景 3。

扩容后应**零代码修改**；如需改代码，说明当初的设计哪里写死了，并一并修正。

---

## 5. 安全与操作边界（必须遵守）

- 对 Hub **只允许**读写 AnywhereUSB 服务的分组与客户端相关配置；不得修改网络、管理员账号、固件、防火墙，不得重启或恢复出厂设置。如确有需要，先征得用户同意。
- 任何写操作前先备份（阶段 0 的 backups），写后读校验；提供 `python -m server.restore <backup>` 恢复脚本。
- 不在日志、代码、提交记录中输出密码或 token。
- 演示环境允许自签证书（httpx `verify=False` 仅由配置项 `hub_tls_verify: false` 打开），在 README 中注明生产环境需使用受信证书。
- 不确定的设备行为（键名、授权是否实时生效、CLI 参数）一律先探测或询问用户，并记录到 docs，不要猜。

---

## 6. 常用命令

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml && cp .env.example .env   # 填写后
pytest -q                                                    # 单元测试（全 mock）
uvicorn server.app:app --host 0.0.0.0 --port 8443            # 启动服务器
python agent/agent.py --config agent.yaml --mock-manager     # Ubuntu 上模拟代理
```

## 7. 完成标准

- 第一步：`docs/api-test-report.md` 中各项均有结论；`pytest` 全部通过，其中 mock 测试覆盖 **2 台 Hub + 2 个代理**（证明扩展逻辑，即使真实环境只有 1 + 1）；真实环境下完成场景 1、2、4、5、6，场景 3 用模拟代理完成。
- 第二步：新增设备后无需改代码，真实环境完成全部 7 个场景。
- README 说明部署步骤、已知限制（每台 Hub Client ID 列表上限 255 条等）与生产化建议（mTLS、高可用、LDAP/AD 对接）。
