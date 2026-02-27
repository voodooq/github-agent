# GitHub MCP Agent 快捷启动脚本 (PowerShell 版)

$ANACONDA_PATH = "D:\anaconda3"
$PYTHON_EXE = "$ANACONDA_PATH\python.exe"

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host " GitHub MCP Agent 快捷启动" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan

if (-not (Test-Path $PYTHON_EXE)) {
    Write-Error "找不到 Python 解释器: $PYTHON_EXE`n请检查脚本中的 ANACONDA_PATH 配置。"
    Read-Host "请按回车键退出..."
    exit 1
}

Write-Host "[信息] 正在使用 Anaconda Python 启动主程序..." -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Cyan
try {
    & $PYTHON_EXE main.py
}
catch {
    Write-Error "程序运行过程中发生异常"
}

Write-Host ""
Read-Host "请按回车键退出..."
