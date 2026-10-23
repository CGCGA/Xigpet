@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m PyInstaller --noconfirm --distpath . --workpath build packaging\Xigpet.spec
if errorlevel 1 (
    echo 打包失败。请先运行: python -m pip install pyinstaller
    pause
    exit /b 1
)
echo 已生成 Xigpet.exe，可直接双击运行。
pause
