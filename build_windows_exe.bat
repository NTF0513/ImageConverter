@echo off
chcp 65001 >nul
set SCRIPT=image_text_converter_pro.py

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 Python Launcher py.exe。请安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
    pause
    exit /b 1
)

echo [0/3] Checking Python 3.10+...
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] 当前默认 Python 3 版本低于 3.10。请安装 Python 3.10/3.11/3.12 后重试。
    py -3 --version
    pause
    exit /b 1
)
py -3 --version

echo [1/3] Installing dependencies with Python 3...
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements_image_converter.txt
if errorlevel 1 pause & exit /b 1

echo [2/3] Building EXE with PyInstaller...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SCP_ImageText_Converter "%SCRIPT%"
if errorlevel 1 pause & exit /b 1

echo [3/3] Done.
echo EXE path: dist\SCP_ImageText_Converter.exe
pause
