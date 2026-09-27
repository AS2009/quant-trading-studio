#Requires -Version 5.1
<#
.SYNOPSIS
    QuantTrading Studio —— Windows **安装包**构建脚本（Inno Setup 6+）。

.DESCRIPTION
    流程：
      0) 环境：找 ISCC.exe、读版本号、确认图标
      1) 确保 onedir 产物存在（没有就调 build_windows.ps1 打一个）
      2) 用 Inno Setup 编译出 QuantTradingStudio-<版本>-Setup.exe
      3) **安装包自检**（和产物自检同等重要）：
         静默安装到临时目录 → 检查开始菜单快捷方式 / 「应用和卸载」登记 / 装好的 exe 能跑 --selftest
         → 静默卸载 → 断言程序目录已删除、而用户数据目录（%LOCALAPPDATA%\QuantTradingStudio）**保留**
      4) 汇总

    设计约定（与 installer\quantstudio.iss 一致）：
      * 默认按当前用户安装（不需要管理员）；安装包自检也按这个路径走，CI 里不需要提权；
      * 卸载默认不删用户数据 —— 第 3 步会真的验证这一点。

.PARAMETER Version
    安装包版本号（默认从 desktop\quantstudio_desktop\__init__.py 读 __version__）。

.PARAMETER Python
    需要现打 onedir 时用的解释器（默认 python，必须是自带 tkinter 的 python.org 构建）。

.PARAMETER SkipBuild
    不检查、也不构建 onedir 产物（缺产物时直接失败）。

.PARAMETER SkipVerify
    跳过静默安装/卸载自检（只出包；CI 不要用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File desktop\build\build_installer.ps1

.EXAMPLE
    pwsh -File desktop\build\build_installer.ps1 -SkipBuild     # 复用已有 onedir 产物
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Python = "python",
    [switch]$SkipBuild,
    [switch]$SkipVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BuildDir     = $PSScriptRoot                                        # desktop\build
$DesktopDir   = Split-Path -Parent $BuildDir                          # desktop
$RepoRoot     = Split-Path -Parent $DesktopDir                        # 仓库根
$InstallerDir = Join-Path $BuildDir "installer"
$IsccScript   = Join-Path $InstallerDir "quantstudio.iss"
$OutputDir    = Join-Path $BuildDir "output"
$Artifacts    = Join-Path $OutputDir "artifacts"
$OnedirDir    = Join-Path $OutputDir "dist\QuantTradingStudio"
$OnedirExe    = Join-Path $OnedirDir "QuantTradingStudio.exe"
$IconFile     = Join-Path $BuildDir "icon.ico"
$ReportPath   = Join-Path $env:TEMP "quantstudio_selftest.txt"
$DataDir      = Join-Path $env:LOCALAPPDATA "QuantTradingStudio"
$TestDir      = Join-Path $env:TEMP ("qts-setup-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$TestLog      = Join-Path $env:TEMP "qts-setup-test.log"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==== $Text" -ForegroundColor Cyan
}

function Get-PathSizeMB([string]$Path) {
    if (-not (Test-Path $Path)) { return 0 }
    $item = Get-Item $Path
    if ($item.PSIsContainer) {
        $sum = (Get-ChildItem -Path $Path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    }
    else {
        $sum = $item.Length
    }
    return [math]::Round(($sum / 1MB), 1)
}

function Find-Iscc {
    $candidates = New-Object System.Collections.Generic.List[string]
    $pf86 = ${env:ProgramFiles(x86)}
    if ($pf86) { $candidates.Add((Join-Path $pf86 "Inno Setup 6\ISCC.exe")) }
    if ($env:ProgramFiles) { $candidates.Add((Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")) }
    if ($env:LOCALAPPDATA) { $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")) }
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw ("找不到 ISCC.exe（Inno Setup 6）。安装： " + [Environment]::NewLine +
           "  choco install innosetup -y                      # Chocolatey（CI 用的就是它）" + [Environment]::NewLine +
           "  winget install -e --id JRSoftware.InnoSetup     # 或者 winget")
}

function Get-SourceVersion {
    $init = Join-Path $DesktopDir "quantstudio_desktop\__init__.py"
    if (-not (Test-Path $init)) { throw "找不到 $init —— 请在完整仓库里运行本脚本。" }
    $text = Get-Content $init -Raw -Encoding UTF8
    $match = [regex]::Match($text, '__version__\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw "没能从 $init 里读到 __version__" }
    return $match.Groups[1].Value
}

function Invoke-GuiSelftest {
    param([string]$ExePath, [string]$Argument)
    if (-not (Test-Path $ExePath)) { throw "找不到可执行文件：$ExePath" }
    Remove-Item $ReportPath -ErrorAction SilentlyContinue
    Write-Host "执行：$ExePath $Argument"
    # console=False 的 GUI 程序用 `&` 调用时 PowerShell 不会等待，必须 Start-Process -Wait
    $proc = Start-Process -FilePath $ExePath -ArgumentList $Argument -Wait -PassThru
    Write-Host "退出码：$($proc.ExitCode)"
    if (Test-Path $ReportPath) {
        Write-Host "---- 自检报告（$ReportPath）----"
        Get-Content $ReportPath -Encoding UTF8 | Write-Host
        Write-Host "---- 报告结束 ----"
    }
    if ($proc.ExitCode -ne 0) {
        throw "安装后的产物自检失败：$Argument（exit=$($proc.ExitCode)）"
    }
}

# =========================================================================== 0. 环境
Write-Step "0/4 环境"
if (-not (Test-Path $IsccScript)) { throw "找不到 $IsccScript" }
$iscc = Find-Iscc
$isccVersion = (Get-Item $iscc).VersionInfo.ProductVersion
Write-Host "仓库根目录 : $RepoRoot"
Write-Host "ISCC       : $iscc（Inno Setup $isccVersion）"
if (-not $Version) { $Version = Get-SourceVersion }
if ($Version -notmatch '^\d+(\.\d+){1,3}$') {
    throw "版本号必须是 1.2 / 1.2.3 / 1.2.3.4 这种形式（VersionInfoVersion 要求纯数字），当前为 '$Version'"
}
Write-Host "版本号     : $Version"
if (-not (Test-Path $IconFile)) { throw "找不到图标 $IconFile（可用 python desktop\build\make_icon.py 生成）" }
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null

# =========================================================================== 1. onedir 产物
Write-Step "1/4 onedir 产物"
if (Test-Path $OnedirExe) {
    Write-Host "复用已有产物：$OnedirDir（$((Get-PathSizeMB $OnedirDir)) MB）"
}
elseif ($SkipBuild) {
    throw "缺少 onedir 产物：$OnedirExe（去掉 -SkipBuild 让脚本自己打）"
}
else {
    Write-Host "没有现成产物，改用 build_windows.ps1 打一份…"
    & (Join-Path $BuildDir "build_windows.ps1") -Python $Python -SkipTests -SkipDeps
    if (-not (Test-Path $OnedirExe)) { throw "打包后仍找不到 $OnedirExe" }
}

# =========================================================================== 2. 编译安装包
Write-Step "2/4 编译 Setup.exe（Inno Setup）"
$setupPath = Join-Path $Artifacts "QuantTradingStudio-$Version-Setup.exe"
& $iscc "/DAppVersion=$Version" "/DSourceDir=$OnedirDir" "/DOutputDir=$Artifacts" "/DAppIcon=$IconFile" $IsccScript
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（exit=$LASTEXITCODE）" }
if (-not (Test-Path $setupPath)) { throw "编译结束但找不到 $setupPath" }
Write-Host "安装包     : $setupPath（$((Get-PathSizeMB $setupPath)) MB）"

# =========================================================================== 3. 安装包自检
Write-Step "3/4 安装包自检（静默安装 → 跑自检 → 静默卸载）"
if ($SkipVerify) {
    Write-Host "已跳过（-SkipVerify）"
}
else {
    if (Test-Path $TestDir) { Remove-Item -Recurse -Force $TestDir }

    Write-Host "-> 静默安装到 $TestDir"
    $installArgs = @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL",
        "/LOG=`"$TestLog`"", "/DIR=`"$TestDir`""
    )
    $proc = Start-Process -FilePath $setupPath -ArgumentList $installArgs -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "静默安装失败（exit=$($proc.ExitCode)）；日志：$TestLog" }

    $installedExe = Join-Path $TestDir "QuantTradingStudio.exe"
    if (-not (Test-Path $installedExe)) { throw "安装后找不到主程序：$installedExe" }
    Write-Host "   主程序      : $installedExe"

    $shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\QuantTrading Studio\QuantTrading Studio.lnk"
    if (-not (Test-Path $shortcut)) { throw "没找到开始菜单快捷方式：$shortcut" }
    Write-Host "   开始菜单    : $shortcut"

    # 严格模式下不能对可能不存在的属性做比较（Uninstall 下有些子键没有 DisplayName），逐个取属性袋
    $uninstallKey = $null
    foreach ($item in (Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue)) {
        $props = Get-ItemProperty $item.PSPath -ErrorAction SilentlyContinue
        if ($props -and $props.PSObject.Properties['DisplayName'] -and
            $props.DisplayName -like "QuantTrading Studio*") {
            $uninstallKey = $props
            break
        }
    }
    if (-not $uninstallKey) { throw "「应用和卸载」里没有登记（HKCU Uninstall 找不到 QuantTrading Studio）" }
    Write-Host ("   卸载登记    : {0}（版本 {1}）" -f $uninstallKey.DisplayName, $uninstallKey.DisplayVersion)
    if ($uninstallKey.DisplayVersion -ne $Version) {
        throw "卸载登记里的版本号是 $($uninstallKey.DisplayVersion)，期望 $Version"
    }

    Write-Host "-> 跑安装后产物自检（同时会创建用户数据目录）"
    Invoke-GuiSelftest -ExePath $installedExe -Argument "--selftest"
    if (-not (Test-Path (Join-Path $DataDir "data"))) {
        Write-Warning "用户数据目录还没出现：$DataDir\data（自检没走到写数据那一步？）"
    }

    Write-Host "-> 静默卸载"
    $uninstaller = Join-Path $TestDir "unins000.exe"
    if (-not (Test-Path $uninstaller)) { throw "找不到卸载程序：$uninstaller" }
    $proc = Start-Process -FilePath $uninstaller -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "静默卸载失败（exit=$($proc.ExitCode)）" }
    Start-Sleep -Seconds 2                      # 卸载进程退出后给它一点时间删目录

    if (Test-Path $TestDir) { throw "卸载后程序目录仍在：$TestDir" }
    Write-Host "   程序目录    : 已删除 ✅"
    if (-not (Test-Path $DataDir)) { throw "用户数据目录被误删了：$DataDir（卸载默认必须保留）" }
    Write-Host "   用户数据    : 保留 ✅（$DataDir）"

    Write-Host "-> 清理自检残留（临时安装目录 + 自检写入的用户数据）"
    if (Test-Path $TestDir) { Remove-Item -Recurse -Force $TestDir -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    Write-Host "安装包自检通过 ✅"
}

# =========================================================================== 4. 汇总
Write-Step "4/4 产物"
Get-ChildItem $Artifacts -Filter "*.exe" | ForEach-Object {
    Write-Host ("{0}  （{1} MB）" -f $_.Name, [math]::Round(($_.Length / 1MB), 1))
}
Write-Host ""
Write-Host "完成 ✅  双击 $setupPath 就是「下一步 → 下一步 → 完成」的安装向导（默认不需要管理员）。" -ForegroundColor Green
Write-Host "用户数据：$DataDir\data（卸载默认保留；可用环境变量 QUANTSTUDIO_DATA_DIR 覆盖）。"
