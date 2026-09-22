#Requires -Version 5.1
<#
.SYNOPSIS
    QuantTrading Studio 桌面版打包脚本（Windows 本地 / GitHub Actions 通用）。

.DESCRIPTION
    流程：可选安装打包依赖 → 核心测试 → 策略规范校验 → 源码自检 →
    PyInstaller(onedir) → 可选 PyInstaller(onefile) → **产物自检**（--selftest / --selftest-gui）。

    产物自检是这里最关键的一步：桌面版是 console=False 的 GUI 程序，stdout 可能为空，
    自检报告同时写入 %TEMP%\quantstudio_selftest.txt，脚本会把它打印出来。

.PARAMETER Python
    使用的 Python 解释器（默认 python）。**必须是自带 tkinter 的 python.org 构建**。

.PARAMETER OneFile
    额外构建单文件版（dist-onefile\QuantTradingStudio.exe）。

.PARAMETER SkipTests
    跳过核心测试与策略校验（只打包；CI 不要用）。

.PARAMETER SkipDeps
    跳过 pip install -r requirements-desktop.txt（已装好 PyInstaller 时更快）。

.PARAMETER Clean
    先删除 desktop/build/output 再打包。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1
.EXAMPLE
    pwsh -File desktop\build\build_windows.ps1 -OneFile -Clean
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$OneFile,
    [switch]$SkipTests,
    [switch]$SkipDeps,
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BuildDir   = $PSScriptRoot                                  # desktop\build
$DesktopDir = Split-Path -Parent $BuildDir                   # desktop
$RepoRoot   = Split-Path -Parent $DesktopDir                 # 仓库根
$OutputDir  = Join-Path $BuildDir "output"
$DistOnedir = Join-Path $OutputDir "dist"
$WorkOnedir = Join-Path $OutputDir "work"
$DistOne    = Join-Path $OutputDir "dist-onefile"
$WorkOne    = Join-Path $OutputDir "work-onefile"
$ReportPath = Join-Path $env:TEMP "quantstudio_selftest.txt"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==== $Text" -ForegroundColor Cyan
}

function Invoke-Python {
    param([Parameter(Mandatory = $true)][string[]]$PyArgs)
    & $Python @PyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败（exit=$LASTEXITCODE）：$Python $($PyArgs -join ' ')"
    }
}

function Invoke-PythonInDir {
    param([string]$WorkingDirectory, [string[]]$PyArgs)
    Push-Location $WorkingDirectory
    try { Invoke-Python -PyArgs $PyArgs } finally { Pop-Location }
}

function Assert-Checkout([string]$Path) {
    if (-not (Test-Path $Path)) {
        throw "找不到 $Path —— 请在仓库根目录（含 backend\ 与 desktop\）中运行本脚本。"
    }
}

# ---- 让 Windows GUI 程序「跑完再看退出码」，而不是启动后立刻返回 ----
function Invoke-GuiSelftest {
    param([string]$ExePath, [string]$Argument)
    if (-not (Test-Path $ExePath)) { throw "找不到打包产物：$ExePath" }
    Remove-Item $ReportPath -ErrorAction SilentlyContinue
    Write-Host "执行：$ExePath $Argument"
    # 注意：console=False 的 GUI 程序用 `&` 调用时 PowerShell 不会等待，必须 Start-Process -Wait
    $proc = Start-Process -FilePath $ExePath -ArgumentList $Argument -Wait -PassThru
    Write-Host "退出码：$($proc.ExitCode)"
    if (Test-Path $ReportPath) {
        Write-Host "---- 自检报告（$ReportPath）----"
        Get-Content $ReportPath | Write-Host
        Write-Host "---- 报告结束 ----"
    }
    else {
        Write-Warning "未找到自检报告文件：$ReportPath"
    }
    if ($proc.ExitCode -ne 0) {
        throw "产物自检失败：$Argument（exit=$($proc.ExitCode)）"
    }
}

function Get-PathSizeMB([string]$Path) {
    if (-not (Test-Path $Path)) { return 0 }
    $item = Get-Item $Path
    $sum = 0
    if ($item.PSIsContainer) {
        $sum = (Get-ChildItem -Path $Path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    }
    else {
        $sum = $item.Length
    }
    return [math]::Round(($sum / 1MB), 1)
}

# =========================================================================== 0. 环境
Write-Step "0/6 环境"
Assert-Checkout (Join-Path $RepoRoot "backend")
Assert-Checkout (Join-Path $DesktopDir "quantstudio_desktop\__main__.py")
Write-Host "仓库根目录 : $RepoRoot"
Write-Host "Python     : $Python"
Invoke-Python -PyArgs @("-c", "import sys; print('版本 :', sys.version)")
Invoke-Python -PyArgs @("-c", "import sys, tkinter; print('tkinter : 可用（Tk %s）' % tkinter.TkVersion)")
if ($Clean -and (Test-Path $OutputDir)) {
    Write-Host "清理 : $OutputDir"
    Remove-Item -Recurse -Force $OutputDir
}

# =========================================================================== 1. 打包依赖
Write-Step "1/6 打包依赖"
if ($SkipDeps) {
    Write-Host "已跳过（-SkipDeps）"
}
else {
    Invoke-PythonInDir -WorkingDirectory $RepoRoot -PyArgs @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-PythonInDir -WorkingDirectory $RepoRoot -PyArgs @("-m", "pip", "install", "-r", "requirements-desktop.txt")
}

# =========================================================================== 2. 测试与校验
Write-Step "2/6 核心测试 / 策略校验"
if ($SkipTests) {
    Write-Host "已跳过（-SkipTests）"
}
else {
    $hasFlask = $true
    try { & $Python -c "import flask" > $null 2>&1 } catch { $hasFlask = $false }
    if ($LASTEXITCODE -ne 0) { $hasFlask = $false }
    if ($hasFlask) {
        Invoke-PythonInDir -WorkingDirectory $RepoRoot -PyArgs @("-m", "unittest", "discover", "-s", "backend/tests", "-v")
    }
    else {
        Write-Warning "未安装 flask（Web 版测试 backend/tests/test_api.py 需要），跳过核心测试；用 python -m pip install flask 后可运行。"
    }
    Invoke-PythonInDir -WorkingDirectory $RepoRoot -PyArgs @("scripts/check_strategies.py")
}

# =========================================================================== 3. 源码自检
Write-Step "3/6 源码自检（打包前先证明源码可用）"
Invoke-PythonInDir -WorkingDirectory $DesktopDir -PyArgs @("-m", "quantstudio_desktop", "--selftest")
Invoke-Python -PyArgs @("-c", "import quantstudio_desktop, sys; print('桌面版版本 :', quantstudio_desktop.__version__)")

# =========================================================================== 4. 打包
Write-Step "4/6 PyInstaller 打包"
$specs = @(
    @{ Spec = "desktop/build/quantstudio.spec";          Dist = $DistOnedir; Work = $WorkOnedir }
)
if ($OneFile) {
    $specs += @{ Spec = "desktop/build/quantstudio-onefile.spec"; Dist = $DistOne; Work = $WorkOne }
}
foreach ($item in $specs) {
    Write-Host "-> $($item.Spec)"
    Invoke-PythonInDir -WorkingDirectory $RepoRoot -PyArgs @(
        "-m", "PyInstaller", $item.Spec, "--noconfirm",
        "--distpath", $item.Dist, "--workpath", $item.Work
    )
}

# =========================================================================== 5. 产物自检
Write-Step "5/6 产物自检（关键）"
$exe = Join-Path $DistOnedir "QuantTradingStudio\QuantTradingStudio.exe"
Invoke-GuiSelftest -ExePath $exe -Argument "--selftest"
Invoke-GuiSelftest -ExePath $exe -Argument "--selftest-gui"
if ($OneFile) {
    $exeOne = Join-Path $DistOne "QuantTradingStudio.exe"
    Invoke-GuiSelftest -ExePath $exeOne -Argument "--selftest"
}

# =========================================================================== 6. 汇总
Write-Step "6/6 产物"
$onedirExe = Join-Path $DistOnedir "QuantTradingStudio\QuantTradingStudio.exe"
Write-Host ("onedir 目录 : {0}  （{1} MB）" -f (Join-Path $DistOnedir "QuantTradingStudio"), (Get-PathSizeMB (Join-Path $DistOnedir "QuantTradingStudio")))
Write-Host ("onedir 主程序: {0}  （{1} MB）" -f $onedirExe, (Get-PathSizeMB $onedirExe))
if ($OneFile) {
    $oneExe = Join-Path $DistOne "QuantTradingStudio.exe"
    Write-Host ("onefile     : {0}  （{1} MB）" -f $oneExe, (Get-PathSizeMB $oneExe))
}
Write-Host ""
Write-Host "打包完成 ✅  双击 onedir 里的 QuantTradingStudio.exe 即可运行（首次启动稍慢）。" -ForegroundColor Green
Write-Host "数据目录：%LOCALAPPDATA%\QuantTradingStudio\data（可用环境变量 QUANTSTUDIO_DATA_DIR 覆盖）。"
