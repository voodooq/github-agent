@echo off
chcp 65001 >nul
echo ===================================================
echo  GitHub MCP Agent 快捷启动脚本
echo ===================================================

:: 设置 Anaconda 安装路径
set ANACONDA_PATH=D:\anaconda3
set PYTHON_EXE=%ANACONDA_PATH%\python.exe

:: 检查 Python 解释器是否存在
if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到系统中的 Python 解释器: %PYTHON_EXE%
    echo 请检查脚本中的 ANACONDA_PATH 配置。
    pause
    exit /b 1
)

:: 启动应用程序
echo [信息] 正在使用 Anaconda Python 启动主程序...
echo ===================================================
"%PYTHON_EXE%" main.py

echo.
pause
