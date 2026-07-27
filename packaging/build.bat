@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  build.bat - 一键打包：PyInstaller -> 便携 zip -> Inno 安装包
REM  用法：双击运行。产物在 dist\ 下：
REM    dist\oimimo\                    onedir 程序目录
REM    dist\oimimo-portable-win64.zip  便携版
REM    dist\oimimo-setup.exe           安装包
REM ============================================================

set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo [1/3] PyInstaller 打包（清空重建 dist\oimimo）...
python -m PyInstaller --noconfirm --clean oimimo.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    pause
    exit /b 1
)

echo.
echo [2/3] 压缩便携版 zip...
if exist "dist\oimimo-portable-win64.zip" del "dist\oimimo-portable-win64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\oimimo' -DestinationPath 'dist\oimimo-portable-win64.zip' -Force"
if errorlevel 1 (
    echo [错误] zip 压缩失败
    pause
    exit /b 1
)

echo.
echo [3/3] 编译 Inno Setup 安装包...
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [错误] 未找到 Inno Setup 6，请先安装：winget install JRSoftware.InnoSetup
    pause
    exit /b 1
)
"%ISCC%" /Q packaging\installer.iss
if errorlevel 1 (
    echo [错误] Inno Setup 编译失败
    pause
    exit /b 1
)

echo.
echo ============ 打包完成 ============
echo   dist\oimimo-portable-win64.zip
echo   dist\oimimo-setup.exe
echo ==================================
pause