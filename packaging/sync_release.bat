@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  sync_release.bat - 开发版 -> 上传版 单向同步脚本
REM  用法：双击运行。同步完成后列出 git 变更清单供核对。
REM  注意：方向永远是 开发版 -> 上传版，不要在上传版里直接改代码！
REM ============================================================

REM 开发版文件夹（源）
set "SRC=E:\vibecoding\QODER项目\qoder开发0.2版本迁移vscode开发中\.qoder\oimimo画师排单助手开发版_v1"

REM 上传版仓库根目录（目标）= 本脚本所在目录的上一级
set "DST=%~dp0.."

if not exist "%SRC%\launcher.py" (
    echo [错误] 找不到开发版目录：%SRC%
    echo 如果开发版文件夹移动过，请编辑本脚本顶部的 SRC 路径。
    pause
    exit /b 1
)

echo.
echo [1/4] 同步核心代码文件...
robocopy "%SRC%" "%DST%" app.py db.py models.py image_processor.py launcher.py requirements.txt run.bat /NJH /NJS /NDL /NP

echo [2/4] 同步 templates\ （镜像模式，自动删除已废弃文件）...
robocopy "%SRC%\templates" "%DST%\templates" /MIR /NJH /NJS /NDL /NP

echo [3/4] 同步 static\ （镜像模式，排除调试截图）...
robocopy "%SRC%\static" "%DST%\static" /MIR /XF receipt_*.png tooltip_test.png _verify_*.png /NJH /NJS /NDL /NP

echo [4/4] 同步 docs\ 三份架构文档...
robocopy "%SRC%" "%DST%\docs" ARCHITECTURE.md DESIGN.md PROJECT.md /NJH /NJS /NDL /NP

echo.
echo ============================================================
echo  同步完成。以下内容【不会】被同步（按设计排除）：
echo    - orders.db / *.db.bak_* / oimimo.db（数据库，含隐私）
echo    - uploads\ exports\ logs\ __pycache__\
echo    - 开发版 CHANGELOG.md（上传版单独维护面向用户的版本记录）
echo    - _verify_*.png / receipt_*.png 等调试截图
echo ============================================================
echo.
echo  git 变更清单（请核对后再 commit）：
cd /d "%DST%"
git status --short 2>nul
echo.
pause
