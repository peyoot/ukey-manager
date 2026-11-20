# AnywhereUSB Manager CLI 笔记（vm-win-1，服务模式，v3.1.39.6）

> AI 生成草稿，需 Digi 工程师审核。以下为实测；未测项标"待测"。
> 注：`help` 输出首行商标符号在 PowerShell 中显示为乱码（"庐"即 ®），是控制台编码问题，解析输出时需注意编码。

## 1. `help` 命令清单（实测）

| 命令 | 说明 |
|---|---|
| `LIST [FULL]` | 列出 Hub、分组、设备 |
| `AUTOCONNECT GROUP,<address>` / `AUTOCONNECT CLEAR GROUP,<address>` / `AUTOCONNECT CLEAR ALL` | 自动连接设置 |
| `KNOWN HUB ADD,<address>[:port]` / `REMOVE,<address>[:port]` / `REMOVE ALL` / `KNOWN HUB LIST` | 已知 Hub 列表 |
| `HUB INFO,<hub name>` / `GROUP INFO,<address>` / `DEVICE INFO,<address>` | 详情 |
| `CONNECT GROUP,<address>` / `DISCONNECT GROUP,<address>` | 连接 / 断开分组 |
| `CONNECT DEVICE,<address>` / `DISCONNECT DEVICE,<address>` | 设备级（须先连接分组） |
| `HUB NAME` / `GROUP NAME` / `DEVICE NAME` | 本地命名 |
| `HIDDEN HUB ADD/REMOVE/REMOVE ALL/LIST` | 隐藏 Hub |
| `POWER CYCLE,address` | 设备断电重启 |
| `REFRESH HUBS` | 刷新 Hub 连接 |
| `AUTOFIND[,ON\|,OFF]` | 自动发现开关 |
| **`SET CLIENTID,<clientId>`** | **设置 Client ID（用户或服务）** |
| `SET KEEPALIVES,<retry>,<timeout>` | 保活参数 |
| `CERTIFICATE LIST` / `CERTIFICATE REMOVE,<hub cert.pem>` | Hub 证书 |
| `SHOW LOG` / `CLEAR LOG` | 日志 |
| `EXIT` | 关闭 Manager |
| `HELP` / `HELP EXAMPLES` | 帮助 |

## 2. 对设计的影响

1. **Client ID 用 `SET CLIENTID,<id>` 设置**，不必手写 `awusb.ini`；需实测是否持久、是否需重启服务。
2. `KNOWN HUB ADD` 的参数是 `<address>[:port]`——**地址形式（IP、序列号还是主机名）待测**，CLAUDE.md 写的 `AW08-017363` 未验证。
3. `HUB INFO,<hub name>` 很可能给出型号/序列号/固件，可解决 Hub 识别问题（待测）。
4. `LIST` 在服务模式末行为 `AnywhereUSB Manager is running as a service`，首行含 `Client ID:`。
5. `POWER CYCLE` 命令存在，演示时可作为附加功能，但不在本项目范围。

## 3. 待测项

- [x] `SET CLIENTID`：返回 `OK`，立即生效，重启服务后保留，自动生成 awusb.ini
- [x] `KNOWN HUB ADD,10.70.1.208`：返回 `OK`；`KNOWN HUB LIST` 显示 `10.70.1.208:18574`（**IP 形式可用**，默认端口 18574）；`AUTOFIND,OFF` 返回 `OK`，`LIST` 显示 `Autofind: disabled`
- [ ] `LIST` / `LIST FULL` 输出格式（含 Hub、分组、设备）
- [ ] `HUB INFO` 输出（型号、序列号、固件）
- [ ] `CONNECT GROUP,<address>` 的确切地址格式与返回；未授权 / 被占用报错；退出码
- [ ] 服务模式下 CLI 所需权限（普通用户 vs 管理员）

## 4. 现象与根因：添加 Hub 后 `LIST FULL` 为空（已定位）

- 添加 Hub 并刷新后 `LIST FULL` 只有表头。`Test-NetConnection 10.70.1.208 -Port 18574` 成功（TcpTestSucceeded=True），`REFRESH HUBS` 返回 `OK`，PowerShell 为管理员（`IsInRole`=True）。
- `SHOW LOG`（实测）关键行：
  - `10.70.1.208:18574 connected as connection N (secure)` —— TLS 连接建立成功；
  - `Connection 1 closed: Unregistered Client Id, error 10058` —— **Hub 拒绝：Client ID 未登记**；
  - 之后每约 30 秒重试一次，每次 `Server error 1, closing connection N`，伴随无害的 `tls_retry_write_records failure`（Hub 主动断开的后果）。
- **根因**：Hub 的 `service.anywhereusb.clients` 为空，`ukey-vm-win-1` 未登记，Hub 在握手后关闭连接，所以看不到任何分组。属预期行为，不是网络或证书问题。
- 其他日志观察：
  - 首次启动记录 `Client certificate not found, generating self-signed cert`，客户端证书自动生成于 `awusbcerts` 同级目录 `awusb_client_cert.pem`；
  - `Cannot enumerate files '...\awusbcerts\*.pem'` 为无 Hub 证书时的良性错误；
  - 日志中的中文乱码（鎿嶄綔…）是 Windows 错误文本的编码问题，等价于"操作成功完成"；
  - **Manager 会持续重试**，因此 Hub 上登记后无需手动操作，下一次重试即可看到分组。
- 结论：下一步需在 Hub 上把 `ukey-vm-win-1` 加入客户端列表（阶段 0.5 A10），再复测 `LIST FULL`。

## 5. 登记后复测（实测，2026-11-20 05:11 之后）

在 Hub 上登记 `ukey-vm-win-1`（`groups` 仍为空）约 30 秒后：

- 日志不再出现 `Unregistered Client Id`，出现 `10.70.1.208:18574 connected as connection 28 (secure)`；
- `LIST FULL` 现在列出 Hub，**格式如下**（代理解析用）：

```
 AW08-017363 (10.70.1.208:18574)
  NAME: AW08-017363
  MODEL: AnywhereUSB 8 Plus
  VERSION: 26.2.148.166 awusb -- Mon, 24 Aug 2026 00:50:09 +0000
  STATE: Active (secure)
  ADDRESS: 10.70.1.208 (SSL Subject:... ,Issuer:...) (10.70.1.208)
  PORT: 18574
  CONNECTED FOR: 197 sec
  CONNECTION ID: 28
  INTERFACE: eth1
  SERIAL NUMBER: AW08-017363
  AUTOCONNECT: disabled
```

- **Hub 识别问题解决**：型号 `AnywhereUSB 8 Plus`、序列号 `AW08-017363`、固件 `26.2.148.166`（2026-08-24 构建），与 config.yaml 一致。Hub 侧 `config.cgi` 读不到这些，**型号/序列号/固件应从 Manager `LIST FULL` 读取**。
- 未授予分组时，Hub 下面没有分组和设备，只显示 Hub 本身。
- 设置 `groups` 之前，Hub 上的"可见性"由客户端列表条目决定：登记 → 可连接 Hub；授予分组 → 才看到分组与设备。
- 日志中仍有早期 `Server error 1` 记录，是登记前的重试；时间戳 05:11:15 起连接成功。
- 待测：授予分组后 `LIST FULL` 的分组/设备格式，`CONNECT GROUP` 地址形式（预计 `AW08-017363.<N>`）。
