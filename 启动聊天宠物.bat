@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0main.py"
    exit /b
)
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0main.py"
    exit /b
)
echo 需要先安装 Python 3（勾选安装时的 Add Python to PATH）。
pause
