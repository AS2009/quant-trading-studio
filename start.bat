@echo off
setlocal
title Quant Trading Studio
cd /d %~dp0

rem 端口 / 监听地址 / 数据源 均可用环境变量覆盖，例如：set PORT=9000
if not defined PORT set PORT=8000
if not defined HOST set HOST=127.0.0.1
if not defined QUANTSTUDIO_DATA_SOURCE set QUANTSTUDIO_DATA_SOURCE=auto

rem ===== 1. 检测 Python =====
rem 注意：块内不能用 %errorlevel% 比较（百分号在块解析时即被展开），统一用 if errorlevel
set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo.
  echo [错误] 没有找到 Python 命令。
  echo.
  echo 请按以下步骤处理:
  echo   1. 关闭窗口后重新打开一个 cmd 窗口
  echo   2. 输入: python --version 回车
  echo   3. 如果提示 "不是内部或外部命令"，说明 Python 未加入 PATH:
  echo      重新运行 Python 安装包，勾选 "Add Python to PATH"
  echo.
  echo 按任意键关闭本窗口...
  pause >nul
  exit /b 1
)

for /f "delims=" %%v in ('%PY_CMD% --version 2^>^&1') do set "PYVER=%%v"
echo.
echo [1/4] 检测到 Python: %PYVER%

rem ===== 2. 准备虚拟环境 =====
set "VENV_PY="
if exist ".venv\Scripts\python.exe" (
  set "VENV_PY=.venv\Scripts\python.exe"
  echo [2/4] 使用已有虚拟环境 .venv
) else (
  echo [2/4] 正在创建虚拟环境 .venv ...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo [提示] 虚拟环境创建失败，将直接使用系统 Python 运行
  ) else (
    set "VENV_PY=.venv\Scripts\python.exe"
  )
)

if defined VENV_PY (set "RUN_PY=%VENV_PY%") else (set "RUN_PY=%PY_CMD%")

rem ===== 3. 依赖（仅 Flask）并校验 =====
echo [3/4] 检查依赖（首次运行需要联网，请稍候）...
%RUN_PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
  %RUN_PY% -m pip install -r requirements.txt -q --disable-pip-version-check
)
%RUN_PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [错误] 依赖安装失败：当前环境无法导入 flask。
  echo 可手动安装后重新运行本脚本:
  echo   %RUN_PY% -m pip install -r requirements.txt
  echo   %RUN_PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  echo 按任意键关闭本窗口...
  pause >nul
  exit /b 1
)

rem ===== 4. 启动服务 =====
echo.
echo [4/4] 启动成功，请用浏览器访问: http://%HOST%:%PORT%
echo       数据源: %QUANTSTUDIO_DATA_SOURCE%   （离线模式请设 QUANTSTUDIO_OFFLINE=1）
echo       本窗口请保持开启，按 Ctrl+C 停止服务
echo.
cd backend
%RUN_PY% app.py
echo.
echo [提示] 服务已停止（若上方有报错信息，请截图反馈）
echo 按任意键关闭本窗口...
pause >nul
endlocal
