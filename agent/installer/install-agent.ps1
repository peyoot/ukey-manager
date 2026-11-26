<#
.SYNOPSIS
  UKey 客户端代理一键安装（方式 A）。需要管理员 PowerShell。

.DESCRIPTION
  1 下载 agent.exe 并校验 SHA-256（来自服务器 manifest）
  2 生成 C:\ProgramData\UKeyAgent\agent.yaml（仅 SYSTEM 与 Administrators 可读）
  3 agent.exe setup-manager：检测 / 安装 Digi Manager（服务模式）/ 设置 Client ID
  4 agent.exe enroll：向服务器登记（管理员批准或 IP 命中自动批准后即在线）
  5 安装并启动 Windows 服务 UKeyAgent（LocalSystem，开机自启，崩溃自动重启）

  不会修改防火墙 / 远程管理 / 杀毒 / UAC；不会卸载或降级已有 Manager。
  Manager 静默安装与 /i /q /g 属 Digi 知识库给出的非官方支持方式（Digi 向导手动选 Service 模式是备用方案）。

.EXAMPLE
  # 自签证书且尚未导入受信任根：先 -ServerCert 指定服务器证书文件，或用 -SkipCertCheck（首次信任并固定证书，务必核对指纹）
  & ([scriptblock]::Create((irm https://10.70.1.63:8443/agent/install.ps1))) -EnrollmentToken <令牌> -SkipCertCheck
#>
[CmdletBinding()]
param(
    [string]$ServerUrl = "__SERVER_URL__",
    [string]$EnrollmentToken,
    [string]$ServerCert,                    # 服务器证书（PEM/CRT）路径；给出后代理会固定信任它
    [switch]$SkipCertCheck,                 # 首次信任：取服务器证书并固定（会打印指纹和警告）
    [switch]$AssumeYes,                     # 已有 stand-alone Manager 在运行/有自定义配置时，确认切换为服务模式
    [string]$InstallDir = "C:\Program Files\UKeyAgent",
    [string]$DataDir = "C:\ProgramData\UKeyAgent"
)
$ErrorActionPreference = "Stop"
$ServiceName = "UKeyAgent"

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

# ---- 0 管理员检查
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请以管理员身份运行 PowerShell。"
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$uri = [Uri]$ServerUrl
$wsUrl = ($ServerUrl -replace '^https', 'wss' -replace '^http', 'ws').TrimEnd('/') + "/ws/agent"
if (-not $EnrollmentToken) {
    $sec = Read-Host "注册令牌（服务器 .env 的 AGENT_ENROLLMENT_TOKEN）" -AsSecureString
    $EnrollmentToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

New-Item -ItemType Directory -Force $InstallDir, $DataDir, "$DataDir\logs" | Out-Null

# ---- 1 证书：固定信任服务器证书
if (-not ([System.Management.Automation.PSTypeName]'UKeyCertPin').Type) {
Add-Type -TypeDefinition @"
using System.Net.Security; using System.Security.Cryptography.X509Certificates;
public static class UKeyCertPin {
  public static string Thumb = null;
  public static bool Check(object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) {
    if (e == SslPolicyErrors.None) return true;
    return Thumb != null && new X509Certificate2(c).Thumbprint == Thumb;
  }
  public static RemoteCertificateValidationCallback Callback = new RemoteCertificateValidationCallback(Check);
}
"@
}
$pinned = "$DataDir\server.pem"
function Save-Pem($cert, $path) {
    $b64 = [Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks')
    Set-Content -Path $path -Value "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" -Encoding ascii
}
if ($ServerCert) {
    Step "使用指定的服务器证书 $ServerCert"
    $c = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path $ServerCert).Path)
    Save-Pem $c $pinned
    [UKeyCertPin]::Thumb = $c.Thumbprint
} elseif ($SkipCertCheck) {
    Write-Warning "-SkipCertCheck：首次信任服务器证书（TOFU）。请把下面的指纹与服务器上 gen_cert.sh 输出的 SHA-256 指纹核对！"
    $tcp = New-Object Net.Sockets.TcpClient($uri.Host, $uri.Port)
    $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ([Net.Security.RemoteCertificateValidationCallback]{ $true }))
    $ssl.AuthenticateAsClient($uri.Host)
    $c = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
    $ssl.Dispose(); $tcp.Dispose()
    $fp = (Get-FileHash -InputStream ([IO.MemoryStream]::new($c.RawData)) -Algorithm SHA256).Hash
    Write-Host "服务器证书 SHA-256 指纹: $fp"
    Save-Pem $c $pinned
    [UKeyCertPin]::Thumb = $c.Thumbprint
}
[Net.ServicePointManager]::ServerCertificateValidationCallback = [UKeyCertPin]::Callback
$caLine = if (Test-Path $pinned) { "server_ca_cert: '$pinned'" } else { "server_ca_cert: null" }

# ---- 2 manifest 与 agent.exe（校验哈希）
Step "读取服务器 manifest"
$manifest = Invoke-RestMethod "$($ServerUrl.TrimEnd('/'))/agent/manifest.json" -UseBasicParsing
if (-not $manifest.agent.available) { throw "服务器上没有 agent.exe（先在构建机构建并放到服务器 dist/）" }
$tmp = Join-Path $env:TEMP "ukey-agent.exe"
Step "下载 agent.exe"
Invoke-WebRequest "$($ServerUrl.TrimEnd('/'))/agent/agent.exe" -OutFile $tmp -UseBasicParsing
$got = (Get-FileHash $tmp -Algorithm SHA256).Hash
if ($got -ne $manifest.agent.sha256.ToUpper()) { Remove-Item $tmp -Force; throw "agent.exe 哈希不符：期望 $($manifest.agent.sha256)，实际 $got" }
Write-Host "agent.exe SHA-256 校验通过 ($got)"
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq 'Running') { Stop-Service $ServiceName -Force; Start-Sleep 2 }
Copy-Item $tmp "$InstallDir\agent.exe" -Force
Remove-Item $tmp -Force
$exe = "$InstallDir\agent.exe"

# ---- 3 配置（权限收紧到 SYSTEM + Administrators）
Step "写入配置 $DataDir\agent.yaml"
$cfgPath = "$DataDir\agent.yaml"
$keepToken = ""
if ((Test-Path $cfgPath) -and (Select-String -Path $cfgPath -Pattern '^agent_token:\s*\S+' -Quiet)) { $keepToken = (Select-String -Path $cfgPath -Pattern '^agent_token:\s*(\S+)').Matches[0].Groups[1].Value }
$yaml = @"
server_url: $wsUrl
api_url: $($ServerUrl.TrimEnd('/'))
$caLine
enrollment_token: '$EnrollmentToken'
agent_token: '$keepToken'
manager:
  exe_path: 'C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe'
  client_id: null
local_ui_port: 47800
log_dir: '$DataDir\logs'
"@
[IO.File]::WriteAllText($cfgPath, $yaml, (New-Object Text.UTF8Encoding $false))
icacls $DataDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

# ---- 4 Manager：检测 / 安装 / 服务模式 / Client ID
Step "setup-manager"
$setupArgs = @("setup-manager", "--config", $cfgPath)
if ($AssumeYes) { $setupArgs += "--yes" }
if ($manifest.manager.available -and $manifest.manager.verified) { $setupArgs += @("--sha256", $manifest.manager.sha256) }
& $exe @setupArgs
$rc = $LASTEXITCODE
if ($rc -eq 3) { throw "检测到 stand-alone Manager 正在运行或有自定义配置。确认可以切换为服务模式后，加 -AssumeYes 重新运行。" }
if ($rc -ne 0) { throw "setup-manager 失败（退出码 $rc）。日志：$DataDir\logs" }

# ---- 5 登记
Step "向服务器登记 (ENROLL)"
& $exe enroll --config $cfgPath
if ($LASTEXITCODE -eq 0) { Write-Host "已获批准" } else { Write-Host "已提交，等待管理员在控制台批准（批准后代理会自动继续）" }

# ---- 6 服务
Step "安装并启动服务 $ServiceName"
$bin = "`"$exe`" service-run --config `"$cfgPath`""
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep 3
}
New-Service -Name $ServiceName -BinaryPathName $bin -DisplayName "UKey Agent" -Description "UKey 管理服务器的客户端代理（演示）" -StartupType Automatic | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
Start-Service $ServiceName
Start-Sleep 3
Get-Service $ServiceName | Format-Table Name, Status, StartType -AutoSize
Write-Host "完成。日志：$DataDir\logs\agent.log；命令行菜单：& '$exe' ui --config '$cfgPath'" -ForegroundColor Green
