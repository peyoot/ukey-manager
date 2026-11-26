<#
.SYNOPSIS
  卸载 UKey 代理：删除服务、C:\ProgramData\UKeyAgent\ 和安装目录。
  默认保留 Digi Manager；加 -RemoveManager 才卸载它（会切回前先尝试 awusbmanager.exe /u）。
#>
[CmdletBinding()]
param(
    [switch]$RemoveManager,
    [string]$InstallDir = "C:\Program Files\UKeyAgent",
    [string]$DataDir = "C:\ProgramData\UKeyAgent"
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "请以管理员身份运行 PowerShell。" }

if (Get-Service UKeyAgent -ErrorAction SilentlyContinue) {
    Stop-Service UKeyAgent -Force -ErrorAction SilentlyContinue
    sc.exe delete UKeyAgent | Out-Null
    Write-Host "已删除服务 UKeyAgent"
}
Start-Sleep 2
Remove-Item -Recurse -Force $InstallDir, $DataDir -ErrorAction SilentlyContinue
Write-Host "已删除 $InstallDir 与 $DataDir"

if ($RemoveManager) {
    $mgr = "C:\Program Files\Digi\AnywhereUSBManager\awusbmanager.exe"
    if (Test-Path $mgr) { & $mgr /u | Out-Null }
    $entry = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "*AnywhereUSB*" } | Select-Object -First 1
    if ($entry -and $entry.UninstallString) {
        Write-Host "卸载 Manager：$($entry.DisplayName)"
        if ($entry.UninstallString -match 'msiexec') {
            $guid = [regex]::Match($entry.UninstallString, '\{[0-9A-Fa-f-]+\}').Value
            Start-Process msiexec.exe -ArgumentList "/x", $guid, "/qn" -Wait
        } else {
            Start-Process cmd.exe -ArgumentList "/c", "`"$($entry.UninstallString)`" /S" -Wait
        }
    } else { Write-Host "没有找到 Manager 的卸载入口，请在“应用和功能”中手动卸载。" }
} else {
    Write-Host "已保留 Digi AnywhereUSB Manager（如需卸载加 -RemoveManager）。"
}
