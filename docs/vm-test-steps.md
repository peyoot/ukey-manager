# vm-win-1 上的 Manager 测试步骤（阶段 0.5 B、C 组）

> AI 生成草稿，需 Digi 工程师审核。所有命令在 vm-win-1（10.70.1.92）的**管理员 PowerShell** 中执行。
> 静默安装与 `/i /q /g` 是 Digi 知识库给出的非官方支持方式，可能随版本变化。
> 请把每步输出贴回给 Claude，用于填写 `manager-install-notes.md` 和 `manager-cli-notes.md`。

## 0. 前置

1. **给 VM 打快照**。如果还没有装 Python，快照命名 `S0-clean`；已装好构建环境则用 `S1-build-env`。测试后可回滚。
2. 记录整个会话，方便贴回：

```powershell
New-Item -ItemType Directory -Force C:\dev\ukey-test | Out-Null
Start-Transcript -Path C:\dev\ukey-test\manager-test.txt -Append
```

3. 取得安装包。Windows 10 自带 `scp`：

```powershell
cd C:\dev\ukey-test
scp robin@10.70.1.63:~/mygit/ukey-manager/dist/40003045_Win64-10.1.38.188_T.exe .
Get-FileHash .\40003045_Win64-10.1.38.188_T.exe -Algorithm SHA256
```

哈希必须是（与 `config.yaml` 一致，服务器上已核对）：

```
C21B97C8FA7E8765503EE648654D4A59BF12FFE82447F1856AC65A1C21272546
```

> 比对时不区分大小写；不一致则停止，不要安装。

## 1. 安装前检查（B 组）

```powershell
Test-Path "C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe"
Get-Service | Where-Object DisplayName -like "*AnywhereUSB*"
```

预期：未安装则第一条为 False，第二条无输出。已安装请贴出结果，先别继续。

## 2. 静默安装

```powershell
$p = Start-Process -FilePath .\40003045_Win64-10.1.38.188_T.exe -ArgumentList '/S','/v/qn' -Wait -PassThru
"ExitCode = $($p.ExitCode)"
Test-Path "C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe"
```

记录：退出码、是否弹出驱动签名/信任提示（若弹窗卡住，截图或描述，不要自行点"始终信任"之外的操作）、安装路径。

## 3. 安装后默认模式

```powershell
cd "C:\Program Files\Digi\AnywhereUSBManager"
Get-Service | Where-Object DisplayName -like "*AnywhereUSB*"
.\awusbmanager.exe LIST
```

预期：安装后为 stand-alone 模式，无服务。贴出 `LIST` 末尾是否提示运行方式。

## 4. 切换为服务模式

```powershell
.\awusbmanager.exe /i /q /g
"ExitCode = $LASTEXITCODE"
Get-Service | Where-Object DisplayName -like "*AnywhereUSB*" | Format-List Name,DisplayName,Status,StartType
Test-Path "C:\Windows\System32\config\systemprofile\AppData\Roaming\AnywhereUSB\"
Get-ChildItem "C:\Windows\System32\config\systemprofile\AppData\Roaming\AnywhereUSB\" -ErrorAction SilentlyContinue
```

记录：服务内部 `Name`（显示名应为 "Digi AnywhereUSB Manager service"）、退出码、配置目录内容。回退命令（仅在需要时）：`.\awusbmanager.exe /u`。

## 5. ClientId 写入是否生效

先查看现有配置（不要贴出与本测试无关的内容）：

```powershell
$ini = "C:\Windows\System32\config\systemprofile\AppData\Roaming\AnywhereUSB\awusb.ini"
Get-Content $ini
```

写入 ClientId 并重启服务（把 `<服务Name>` 换成第 4 步看到的名称）：

```powershell
notepad $ini
```

在记事本里，于 `[Settings]` 段内加入一行 `ClientId=ukey-vm-win-1`，保存关闭，然后：

```powershell
Restart-Service "<服务Name>"
.\awusbmanager.exe LIST
```

记录：重启是否成功，`LIST` 是否反映新的 Client ID（若不显示，说明 CLI 看不到，需另寻验证方式）。

## 6. 连接 Hub 并读取（C 组）

```powershell
.\awusbmanager.exe KNOWN HUB ADD,AW08-017363
.\awusbmanager.exe AUTOFIND,OFF
.\awusbmanager.exe LIST
.\awusbmanager.exe LIST FULL
.\awusbmanager.exe help
```

`LIST FULL` 的完整输出最重要：其中应有 Hub 序列号、型号、各端口设备与分组。若 `KNOWN HUB ADD` 需要 IP 形式（如 `KNOWN HUB ADD,10.70.1.208`），请同时试并告知。

## 7. 连接 / 断开 / 报错（只用端口 1 的演示 U 盘）

目前 Hub 的 `clients` 列表为空，**授权前 CONNECT 预期失败**，这一步正好用来记录"未授权"的报错：

```powershell
.\awusbmanager.exe CONNECT GROUP,AW08-017363.1
"ExitCode = $LASTEXITCODE"
.\awusbmanager.exe LIST
```

确切的 `CONNECT GROUP` / `DISCONNECT GROUP` 语法以 `help` 输出为准，如与上面不同请按 `help` 调整并告知。授权成功后的连接测试等 Hub 写入实验完成后再做，**现在不要在 Hub 页面上手工改授权**。

## 8. 收尾

```powershell
Stop-Transcript
```

把 `C:\dev\ukey-test\manager-test.txt` 的内容（或各步输出）贴回。若中途卡住，可用快照回滚。

## 回报清单

- [ ] 第 0 步哈希是否一致
- [ ] 第 1 步安装前状态
- [ ] 第 2 步退出码、有无弹窗、路径
- [ ] 第 3 步默认模式
- [ ] 第 4 步服务名、退出码、配置目录
- [ ] 第 5 步 ClientId 是否生效
- [ ] 第 6 步 `LIST FULL`、`help` 的完整输出
- [ ] 第 7 步未授权 CONNECT 的报错与退出码
