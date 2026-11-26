# 在 Windows 构建机（vm-win-1）上打包 agent.exe。步骤见 CLAUDE.md 第 4 节。
# 前提：已在仓库目录建好 .venv 并 pip install -r agent\requirements.txt
#   cd C:\dev\ukey-agent ; .\.venv\Scripts\Activate.ps1 ; .\agent\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".\agent\agent.py") -or -not (Test-Path ".\common\protocol.py")) {
    throw "请在仓库根目录运行（需要 agent\ 和 common\ 两个目录）"
}
python --version
python -c "import websockets, httpx, yaml, pydantic, win32service; print('依赖 OK')"
if ($LASTEXITCODE -ne 0) { throw "缺少依赖，请先在虚拟环境里运行：pip install -r agent\requirements.txt" }

Remove-Item -Recurse -Force .\build, .\dist\agent.exe -ErrorAction SilentlyContinue
pyinstaller --noconfirm --onefile --name agent --paths . `
    --hidden-import win32timezone --hidden-import servicemanager --hidden-import win32serviceutil `
    --hidden-import win32service --hidden-import win32event --hidden-import win32api `
    --collect-all pydantic --collect-submodules websockets --collect-data certifi `
    agent_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Resolve-Path .\dist\agent.exe
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host "`n产物: $exe"
Write-Host "SHA-256: $hash"
& $exe version
if ($LASTEXITCODE -ne 0) { throw "agent.exe 无法启动（上面的 Traceback 说明缺什么）。构建产物不可用，不要拷到服务器。" }
Write-Host "`n下一步：scp dist\agent.exe <user>@10.70.1.63:~/mygit/ukey-manager/dist/"
Write-Host "提示：PyInstaller 的 exe 偶尔被 Windows Defender 误报；演示时可加入排除项，正式交付建议代码签名。"
