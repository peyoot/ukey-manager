# 真实环境验证报告（hub-a + vm-win-1）

> AI 生成草稿，需 Digi 工程师审核。区分三类结论：**已验证**（有日志/输出为证）、**仅自动化测试**（mock，未在真实设备上跑）、**未验证**。
> 环境：Ubuntu 24.04 `10.70.1.63`（服务器）；Hub：AnywhereUSB 8 Plus `AW08-017363`，固件 26.2.148.166；VM：Windows 10 `10.70.1.92`（主机名 `DESKTOP-8DDON30`，代理 ID `desktop-8ddon30`），Manager 3.1.39.6 服务模式；演示设备：端口 6 = U 盘（映射为 E:），端口 3 = Digi XBee 开发板。
> 时间戳：服务器为 UTC，VM 为 UTC+8。

## 1. 演示场景结果

| # | 场景 | 结论 | 依据 |
|---|---|---|---|
| 1 | 控制台显示 8 个端口与 vm-win-1 在线 | **已验证** | 用户截图：代理页显示 `desktop-8ddon30` 已批准、在线、`service 3.1.39.6`、Client ID `ukey-vm-win-1`、自检正常；端口总览与服务器核对一致 |
| 2 | 用向导为 hub-a 完成分组和 Client ID 配置 | **仅自动化测试 + 页面确认** | 向导后端在 mock Hub 上通过全流程测试（含失败后恢复）；页面由用户在浏览器里确认无问题。**未对真实 hub-a 运行向导的"应用"**；hub-a 的分组、描述、客户端是阶段 0.5 用 REST 手工写入并读回校验的 |
| 3 | vm-win-1 申请端口 6 → 自动授权并连接 | **已验证** | 审计：06:44:28 授权 `ukey-vm-win-1 → group06`，06:44:29 连接成功；用户确认 E: 盘出现并可打开（含 `1.txt`）。多次重复（07:33、07:54）均成功 |
| 4 | 第二个客户端申请同一 UKey → 让出 | **仅自动化测试** | 2 个代理的协议级与真实 socket 测试覆盖让出 / 拒绝 / 无响应；Ubuntu 上 `--mock-manager` 代理在真实 TLS 上跑通登记、批准、CONFIG、心跳。**未用真实第二台 VM** |
| 5 | 控制台强制回收 → VM 断开，Hub 授权撤销 | **已验证** | 审计：06:50:14、07:35:57 `release (forced)`；用户确认 E: 消失、端口收回；先优雅 DISCONNECT（代理日志 `DISCONNECT … OK`）再撤销 |
| 6 | 停止代理服务（模拟崩溃）→ 心跳超时自动回收 | **仅自动化测试 / 未在真实环境确认** | 自动化测试覆盖（代理崩溃后服务器 `sweep` 撤销授权，结束原因 `agent_lost`）。真实 VM 上 `Stop-Service UKeyAgent` 的结果**没有拿到输出** |
| 7 | 审计日志可查看以上全部操作 | **已验证（服务器侧）** | 数据库里有 grant / connect / event / release / register / approve_agent / agent_disconnected 等完整记录；用户打开过审计页 |
| 8 | 向导添加第二台 Hub；一台断电后端口标故障 | **仅自动化测试 / mock** | `dev_console` 加 mock 24 口 Hub 走完向导；Hub 离线只影响自身端口、恢复后还原由 `tests/test_scheduler.py` 覆盖。**没有第二台真实 Hub** |

其他真实环境结果：

| 项 | 结论 | 依据 |
|---|---|---|
| 方式 A 安装（一条命令：装代理、setup-manager、登记、起服务） | **已验证** | 脚本输出：哈希校验通过、`setup-manager` 识别"已是服务模式"并保留已有 Client ID、登记成功、`UKeyAgent` Running / Automatic |
| 升级（重跑安装脚本，保留专属 token 无需重新批准） | **已验证** | 多次升级，代理重新注册（日志 `已注册到服务器`），状态仍为已批准 |
| 服务器重启后代理自动重连 | **已验证** | 07:41:37 断开 → 07:41:47 自动重新注册 |
| 租约到期自动回收 | **未验证** | 租约 08:04:36 到期，未拿到结果；自动化测试覆盖 |
| 重启 VM 后代理自动上线 | **未验证** | 服务为 `Automatic` 启动，尚未重启验证 |
| 已装 stand-alone Manager 时识别并切换 | **仅自动化测试** | 真实 VM 一直是服务模式；`/i /q /g` 切换本身在阶段 0.5 手工验证过 |
| 方式 B 远程安装（WinRM） | **未验证** | 默认关闭 |
| 代理服务（`winservice.py`）、PyInstaller 打包、证书固定 | **已验证（能用）** | 服务起得来、多次升级正常、TLS 到服务器正常 |

## 2. 真实环境里发现的问题与修复

| # | 现象 | 原因 | 修复 |
|---|---|---|---|
| 1 | `build.ps1` 解析报错，输出一堆乱码 | Windows PowerShell 5.1 把**无 BOM** 的 UTF-8 `.ps1` 当 ANSI(GBK) 读 | 所有 `.ps1` 加 UTF-8 BOM；服务器下发 `install.ps1` 时去掉 BOM；加测试 |
| 2 | `agent.exe version` 启动即 `No module named 'pydantic'` | `agent/requirements.txt` 漏了 pydantic，PyInstaller 不报错但运行时缺失 | 补依赖；`build.ps1` 构建前检查依赖、构建后自检 exe（失败即抛错）；加"第三方 import 必须在 requirements 里"的测试 |
| 3 | `install-agent.ps1`：无法将 MethodInfo 转换为 RemoteCertificateValidationCallback | PowerShell 不能把方法对象直接转委托 | 在 C# 里直接暴露委托 |
| 4 | （审查中发现，未触发）`sc.exe create` 的 binPath 引号被 5.1 吃掉；`-Encoding utf8` 写 BOM 使 PyYAML 报错 | PowerShell 5.1 已知行为 | 改用 `New-Service`；yaml 无 BOM 写入，读取容忍 BOM |
| 5 | `agent.exe ui` 菜单列出端口时 `UnicodeDecodeError: 'gbk'` | `socket.makefile()` 没指定编码，中文 Windows 默认 GBK，服务返回 UTF-8 中文 | 显式 UTF-8；连不上时返回错误而非 Traceback；加回归测试 |
| 6 | 强制回收后重新申请失败，U 盘不出现（审计 06:54：授权成功、CONNECT 返回 FAILED） | 撤销后再授权，本机 Manager 不一定马上看到该分组 | 代理 CONNECT 失败时 `REFRESH HUBS` 并每 3 秒重试，最多 40 秒；服务器对 CONNECT 单独等 60 秒（`lease.connect_timeout_seconds`）；同 id 重发在执行中被忽略 |
| 7 | 菜单申请报"连接不上本机代理服务 … timed out"，其实申请成功（授权 07:54:37 → 连接 07:54:46） | 修复 6 后申请要约 9 秒，菜单只等 10 秒 | 申请 / 释放等待上限 150 秒并提示"处理中"；日志逐次记录重试 |
| 8 | 查看代理日志出现乱码 | 文件是 UTF-8，`Get-Content` 默认按本地编码读 | 文档里统一用 `-Encoding UTF8` |

教训：**PowerShell 5.1 与中文 Windows 的编码**是主要坑；Linux 上写的 Windows 脚本无法本地验证，必须在真实 VM 上逐步跑。

## 3. 阶段 0.5 的关键实测结论（摘要，详见各 notes）

- Hub 写入：`POST /value?path=..&value=..`，数组 `append=true`，`DELETE /value?path=<数组>.<序号>`（删除后序号前移）；错误也返回 HTTP 200，须检查 body；写入立即生效。
- 端口 N = `groupNN`；Manager 分组地址 = `<序列号>.<分组序号>`（端口 6 → `AW08-017363.6`）。
- **撤销授权 = 同一秒强制断开**；未授权的分组对客户端不可见；未登记的 Client ID 握手后被拒（`Unregistered Client Id`）。
- Manager CLI：成功 `OK`/0，失败 `FAILED`/1；`SET CLIENTID` 立即生效且持久；服务名 `awusb`；静默安装 `/S /v/qn` 退出码 0 且无驱动弹窗。

## 4. 仍待验证 / 遗留事项

1. 场景 6 在真实 VM 上的结果（停代理服务 → 约 45 秒后端口回到空闲、E: 消失、审计出现 `agent_timeout` 与 `release (agent_lost)`）。
2. 租约到期自动回收；重启 VM 后代理自动上线。
3. 对**真实 hub-a** 运行"编辑 Hub"向导的"应用"（写入路径在 mock 与阶段 0.5 手工验证过，向导本身未对真实设备运行）。
4. 第二台真实 VM（场景 4）与第二台真实 Hub（场景 8）；方式 B 远程安装。
5. 设计决定：租约是否改为"在线且设备仍连接时自动续租"。
6. 清理：hub-a 上 `ukey-vm-win-1` 遗留的手工实验授权（`group03`）；服务器数据库里的 `mock-vm-1` 假代理记录。
7. 把已修复的 UI 客户端与重试日志打包进新的 `agent.exe`（目前线上 exe 缺少"申请最多等 150 秒"的修复，用源码运行菜单可绕过）。

## 5. 自动化测试

`pytest -q`：170 个测试通过，全部 mock，覆盖 2 台 Hub + 2 个代理；包含真实 TCP/WebSocket 端到端、Manager 输出解析（用真实输出做夹具）、向导（含失败后恢复）、控制台安全头与实时推送、PowerShell 脚本的已知坑回归检查。
