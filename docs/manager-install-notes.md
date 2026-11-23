# AnywhereUSB Manager 安装与模式测试记录（vm-win-1）

> AI 生成草稿，需 Digi 工程师审核。静默安装与 `/i /q /g` 属 Digi 知识库给出的非官方支持方式。
> 安装包：`40003045_Win64-10.1.38.188_T.exe`，SHA-256 `c21b97c8…272546`（VM 上已核对一致）。
> 步骤清单见 `docs/vm-test-steps.md`。

| # | 项目 | 命令 | 预期 | 实际 | 结论 |
|---|---|---|---|---|---|
| 1 | 安装前状态 | `Test-Path ...\awusbmanager.exe`；`Get-Service` 过滤 AnywhereUSB | 未安装、无服务 | `False`；无服务 | PASS |
| 2 | 静默安装 | `Start-Process <安装包> /S /v/qn -Wait -PassThru` | 退出码 0 | 退出码 **0**；`awusbmanager.exe` 出现在默认路径 `C:\Program Files\Digi\AnywhereUSBManager\` | PASS |
| 2a | 驱动签名/信任提示 | 观察 | 可能弹窗 | **静默安装时没有弹出任何提示**（用户确认） | PASS |
| 3 | 安装后是否有服务 | `Get-Service` 过滤 AnywhereUSB | stand-alone：无服务 | **无服务**（stand-alone 模式） | PASS |
| 3a | 安装后 `LIST` | `awusbmanager.exe LIST` | 列表或提示 | `ERROR: AnywhereUSB Manager is not running.` | 见下 |
| 4 | 切换服务模式 | `awusbmanager.exe /i /q /g` | 创建并启动服务 | 退出码 **0**，无输出；服务已创建并 Running | PASS |
| 5 | 服务名 | `Get-Service` 过滤 AnywhereUSB | 显示名 "Digi AnywhereUSB Manager service" | Name=**`awusb`**，DisplayName=**`Digi AnywhereUSB Manager`**（无 "service" 后缀），Running，Automatic | PASS（与 CLAUDE.md 显示名略有不同，以实测为准） |
| 5a | 服务配置目录 | `Get-ChildItem ...systemprofile\AppData\Roaming\AnywhereUSB\` | 含 awusb.ini | 目录存在，但**只有 `awusb.log`（721 字节），尚无 `awusb.ini`** | 见下 |
| 5b | 服务模式 `LIST` | `awusbmanager.exe LIST` | 以服务方式运行 | 版本 **3.1.39.6**，`Client ID:` 为空；`Autofind: enabled`；末行 `AnywhereUSB Manager is running as a service` | PASS |
| 6 | `SET CLIENTID,ukey-vm-win-1` | `awusbmanager.exe SET CLIENTID,...` | 设置并持久 | 输出 `OK`，退出码 0；`LIST` 首行显示 `Client ID: ukey-vm-win-1`；`awusb.ini` 自动生成（36 字节）；`Restart-Service awusb` 后 Client ID **保留** | PASS |
| 7 | 安装时的权限 | 待确认 | | 用户不确定静默安装时 PowerShell 是否为管理员，待用 `IsInRole` 核对 | 待确认 |

## 发现与对代理实现的影响

1. 静默安装可行：退出码 0，默认路径与 CLAUDE.md 一致。
2. 刚装完的 stand-alone 状态下，没有 Windows 服务，且 CLI `LIST` 返回 `ERROR: AnywhereUSB Manager is not running.`——stand-alone 的 CLI 需要 GUI/后台实例在运行，没有运行时直接报错。
3. 因此 `setup-manager` 的模式检测不能只靠 `LIST` 输出：应组合"可执行文件是否存在 + 服务是否存在 + `LIST` 结果"：
   - 无 exe → missing
   - 有 exe、有服务 → service（再用 `LIST` 判断服务是否健康）
   - 有 exe、无服务 → standalone（或未运行）
4. 已装但未运行时 CLI 退出码与输出尚未记录（建议下次加 `$LASTEXITCODE`）。

## 切换服务模式后的发现

5. 服务名是 `awusb`（代理代码应按服务名 `awusb` 检测，显示名只作辅助）。
6. 判断服务模式：`LIST` 末行 `AnywhereUSB Manager is running as a service`，同时服务 `awusb` 为 Running。
7. `/i /q /g` 静默、无输出，靠 `$LASTEXITCODE` 判断；成功后服务自动启动、开机自启。
8. 全新切换后 `Client ID` 为空，配置目录里还没有 `awusb.ini`，说明 ini 需由代理创建（含 `[Settings]` 与 `ClientId=`），重启服务后用 `LIST` 的首行 `Client ID:` 验证。
9. Manager 版本显示 3.1.39.6，安装包文件名为 10.1.38.188（后者是安装包版本号，二者不同属正常，需 Digi 确认）。
10. `Autofind: enabled`——代理需按 CONFIG 执行 `AUTOFIND,OFF`。

11. `SET CLIENTID,<id>` 实测有效：返回 `OK`，自动创建 `awusb.ini`，重启服务后保留。**代理应使用此命令，不手写 ini。**
12. `SET CLIENTID` 后未重启服务，`LIST` 立即显示新 Client ID，无需重启。
13. **服务模式下可打开 GUI**（实测）：管理员身份运行 `Start-Process "C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe" -Verb RunAs` 可打开 Manager 界面查看设备与连接，无需切回 stand-alone。演示时可用它做可视化。
