# PowerShell 一键打包脚本
$ErrorActionPreference = "Stop"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  正在准备构建 AstrBotScreenAgent.exe 单文件可执行程序..." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 探测可用的 Python 解释器
$pythonExe = $null

if (Test-Path "F:\python3.14.6\python.exe") {
    $pythonExe = "F:\python3.14.6\python.exe"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
}

if (-not $pythonExe) {
    Write-Host "[错误] 未能检测到可用的 Python 解释器！" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "[*] 使用 Python 路径: $pythonExe" -ForegroundColor Green

# 切换工作目录
Set-Location -Path $PSScriptRoot

Write-Host "[*] 检查安装依赖..." -ForegroundColor Yellow
& $pythonExe -m pip install -r requirements.txt

Write-Host "[*] 执行 PyInstaller 打包..." -ForegroundColor Yellow
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
& $pythonExe -X utf8 build_exe.py

Write-Host "`n打包流程执行完毕！" -ForegroundColor Green
pause
