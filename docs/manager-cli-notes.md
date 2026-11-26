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

## 6. 授权后 `LIST FULL`（实测）与分组地址

Hub 上端口 1 单独放入 `group02` 并授权给 `ukey-vm-win-1` 后，`LIST FULL` 在 Hub 下新增：

```
    Group 2 (AW08-017363.2)
      ADDRESS: AW08-017363.2
      GROUP: 2
      NAME: Group 2
      PORTS: 1
      AUTOCONNECT: disabled
      IN USE BY: NO ONE
```

关键发现：
1. **分组地址 = `<序列号>.<分组序号>`，序号来自 Hub 分组名 `groupNN` 的 NN，不是端口号。** 端口 1 在 group02，所以地址是 `AW08-017363.2`，不是 CLAUDE.md 示例的 `.1`。服务器必须维护"端口 ↔ 分组"映射并按分组下发 CONNECT。
2. 授权约 30 秒内 VM 即可见分组（Manager 自动重试，无需手动刷新；这里等待了数十秒到数十分钟，未精确测量）。
3. `IN USE BY: NO ONE` 给出占用方，可用于对账和 RELEASE_ASK 判定。
4. `PORTS: 1` 显示分组包含的端口；分组下尚未列出设备（预计连接分组后出现，待测）。
5. 待测：`CONNECT GROUP,AW08-017363.2` 的返回与设备管理器表现；`DISCONNECT GROUP`；被占用/撤销授权时的行为。

## 7. CONNECT GROUP 实测（2026-11-20）

```
> awusbmanager.exe CONNECT GROUP,AW08-017363.2
OK
ExitCode = 0
```

- `CONNECT GROUP` 成功：输出 `OK`，退出码 0，约 5 秒内 `LIST FULL` 变为 `Group 2 (AW08-017363.2) (In-use by you)`，`IN USE BY: YOU`。
- `GROUP INFO,AW08-017363.2` 输出 5 行：`ADDRESS / GROUP / NAME / PORTS / IN USE BY`（可用于代理轻量查询占用方）。
- 解析提示：分组标题行带 `(In-use by you)` 后缀；占用方取 `IN USE BY:` 行（`NO ONE` / `YOU` / 其他客户端，其他客户端的写法待测）。
- 系统中出现 `AnywhereUSB eXtensible Remote Host Controller`（虚拟主控制器，表示 Manager 驱动工作）。
- **异常：分组下没有设备行，设备管理器也没出现 U 盘。** 原因待确认：端口 1 上是否真的插有设备，或设备尚未枚举。待用户确认物理连接后复测。

## 8. 连接端口 6 的 U 盘（实测）

`CONNECT GROUP,AW08-017363.6` 后（用户贴回的输出）：

- 设备管理器 / PnP：`DiskDrive: VendorC ProductCode USB Device`、`USB: USB 大容量存储设备`、`WPD: E:\`；
- `Get-Disk`：`Number 1, VendorC ProductCode, BusType USB, 约 60 GB`；系统自动分配盘符 **E:**；
- 即远程 USB 设备在 VM 里表现为本地 USB 磁盘，无驱动提示，**端口 6（Group 6）连接成功**。
- 未收到：断开 group02 后的 `LIST FULL` 与连接后 `LIST FULL` 的设备行格式——待补，用于代理解析。

## 9. 撤销授权已连接分组（A12，实测 2026-11-26）

- Hub 上 `DELETE clients.0.groups.<group06 的索引>` 于 03:29:13 返回 ok；VM 日志**同一秒**记录 `Device 2006 UNBOUND from connection 1 (success 1)`。
- 结果：E: 盘立即消失（`Get-Disk` 只剩系统盘）；`LIST FULL` 不再显示 group06，也就是 Hub 不再向该客户端展示未授权的分组。
- **结论：Hub 撤销授权 = 立即强制断开，不等客户端同意。** 兜底回收只靠 `hub.revoke()` 就能生效；但这是强制拔盘，可能丢数据，所以正常释放应先发 DISCONNECT（让 VM 优雅断开）再 revoke。
- 注意：撤销时 VM 上若有文件被占用，行为等同拔 U 盘。

## 10. `LIST FULL` 设备行格式（实测，XBee 开发板在端口 3）

```
    hub-a-p03 (AW08-017363.3)        # 分组名来自 Hub 分组 description
      ADDRESS: AW08-017363.3
      GROUP: 3
      NAME: hub-a-p03
      PORTS: 3
      AUTOCONNECT: disabled
      IN USE BY: NO ONE

        XBee Micro Development Board (AW08-017363.1703)
          ADDRESS: AW08-017363.1703   # 设备地址 ≠ 分组地址，设备序号不等于端口号
          VENDOR: Digi
          VENDOR ID: 0x0403
          PRODUCT: XBee Micro Development Board
          PRODUCT ID: 0x6001
          PORT ON HUB: 3               # 设备所在物理端口
          AUTOCONNECT: disabled
          IN USE BY: NO ONE
          SPEED: FULL
```

- 分组名 `NAME` 随 Hub 分组 `description` 变化（hub-a-p03），**服务器改 description 即可让 VM 上显示可读名称**。
- 设备行出现在分组下（缩进更深），含 `PORT ON HUB`，**可用来判断某端口是否有设备**。
- 设备地址形如 `AW08-017363.1703`（U 盘日志中为 device 2006），其数字不能由端口号推出，代理应解析 `PORT ON HUB`。
- 空分组（如 hub-a-p02）只有分组行，无设备行。
- 连接后设备的 `IN USE BY` 预计为 `YOU`（待用已连接状态补测）。
- 观察：`CONNECTED FOR: 407812 sec`、`CONNECTION ID: 1`：此次 Manager 与 Hub 的连接已持续约 4.7 天（VM 一直在线），期间自动保持。

## 11. 未授权时 CONNECT（实测 2026-11-26）

```
> awusbmanager.exe CONNECT GROUP,AW08-017363.6      # group06 已被 Hub 撤销授权
FAILED
ExitCode = 1
```

- 未授权（分组对该客户端不可见）时：输出 `FAILED`，退出码 **1**，没有更详细的原因文本。
- 代理判断：退出码非 0 或输出含 `FAILED` 即失败；原因需结合 `LIST FULL`（分组是否可见）、`SHOW LOG` 区分。
- 成功为输出 `OK`、退出码 0。

## 12. 连接 / 断开完整流程（端口 6 U 盘，实测 2026-11-26）

| 步骤 | 命令 | 输出 / 退出码 | 观察 |
|---|---|---|---|
| 连接 | `CONNECT GROUP,AW08-017363.6` | `OK` / 0 | 8 秒内分组变 `(In-use by you)`，设备行 `IN USE BY: YOU`、`ASSIGNED VIRTUAL PORT: 128`、`SPEED: SUPERSPEED` |
| 系统 | `Get-Disk` / `Get-Volume E` | USB 磁盘约 60 GB，NTFS | E: 可读，能看到测试文件 `1.txt` |
| 断开 | `DISCONNECT GROUP,AW08-017363.6` | `OK` / 0 | 5 秒内 `IN USE BY: NO ONE`，`Test-Path E:\` 为 False |

- 断开后分组与设备**仍然列出**（`NO ONE`），因为授权仍在；只有撤销授权后才从 `LIST FULL` 消失。
- U 盘设备行含 `SERIAL`（FC1575EA0B448）、`VENDOR ID/PRODUCT ID`，适合在控制台展示。
- 优雅断开（DISCONNECT）与强制断开（撤销授权）的区别见第 9 节。
