; QuantTrading Studio —— Windows 安装包脚本（Inno Setup 6+）
;
; 编译（本地 / CI 都一样；路径都从本文件所在目录算，所以参数用相对路径即可）：
;   ISCC.exe /DAppVersion=1.5.1 ^
;            /DSourceDir=..\output\dist\QuantTradingStudio ^
;            /DOutputDir=..\output\artifacts ^
;            quantstudio.iss
;
; 可用 /D 覆盖的宏：AppVersion / SourceDir / OutputDir / AppIcon / DataNoteFile
;
; 设计要点（改这个文件前先读）：
;   * 默认**按当前用户安装**（PrivilegesRequired=lowest）→ 不弹 UAC，装到
;     %LOCALAPPDATA%\Programs\QuantTrading Studio；向导里仍可切换「为所有用户安装」（会自动提权）；
;   * 用户数据就放在**安装目录**里，按类别分文件夹（v1.5.1 起）：
;       {app}\strategies\   策略 .py（复制进去即可用；首次启动自动放入内置示例与模板）
;       {app}\data\csv\     行情 CSV        {app}\data\level2\  盘口 / 逐笔 CSV
;       {app}\data\cache\   程序缓存        {app}\data\         自选池、模拟盘账本等
;     [Dirs] 里给这些目录放开了 Users 修改权限 —— 即使「为所有用户安装」到 Program Files，
;     普通用户也能直接往 strategies\ 里复制策略文件（不需要管理员）；
;   * **卸载默认不动用户数据**：交互式卸载会问一句「是否同时删除用户数据」，默认「否」；
;     静默卸载永不删数据；[Dirs] 条目带 uninsneveruninstall，空目录也不会被卸载器删掉；
;   * 开始菜单快捷方式 + 可选桌面快捷方式 + 「应用和卸载」里的卸载项（Inno 自动登记，含图标/版本/发布者）；
;   * 安装/卸载前会自动关闭正在运行的 QuantTrading Studio（Inno 的 CloseApplications）；
;   * 升级：AppId 固定 + UsePreviousAppDir，覆盖安装会沿用上次的安装目录（数据在安装目录里，天然沿用）；
;   * **本文件必须是 UTF-8 带 BOM**（否则 Inno 按 ANSI 读，中文会乱码）。
;
; 注意：向导主界面是 Inno 自带的英文（官方未提供简体中文语言包），本脚本里我们自己的
; 字符串（任务说明、启动项、提示）都是中文。需要全中文界面可自备 ChineseSimplified.isl 再补一条 [Languages]。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\output\dist\QuantTradingStudio"
#endif
#ifndef OutputDir
  #define OutputDir "..\output\artifacts"
#endif
#ifndef AppIcon
  #define AppIcon "..\icon.ico"
#endif
#ifndef DataNoteFile
  #define DataNoteFile "安装与数据说明.txt"
#endif
; 内置示例策略 / 模板的**源码位置**（相对本文件：installer → build → desktop → 仓库根）
; 装到 {app}\_seed\（不是 strategies\：那里是用户数据，不能被卸载器登记/删除）；
; 程序首次启动会把它们播种到 strategies\，用户也可以直接复制/改名使用。
#ifndef SeedSourceDir
  #define SeedSourceDir "..\..\..\backend\quantstudio\strategies\local"
#endif
#define MyAppName "QuantTrading Studio"
#define MyAppExe "QuantTradingStudio.exe"
#define MyAppURL "https://github.com/AS2009/quant-trading-studio"
; 用户数据（都在安装目录内，按类分文件夹；见文件头说明）
#define MyDataDir "{app}\data"
#define MyStrategiesDir "{app}\strategies"
; 旧版本（≤ v1.5.0）的用户数据位置：程序首次启动会把内容复制到安装目录（原位置保留），
; 卸载时若用户选择「同时删除用户数据」，这里也一并清掉。
#define MyLegacyDataDir "{localappdata}\QuantTradingStudio"

[Setup]
; AppId 必须保持不变，升级安装才能识别为同一个程序
AppId={{7B2E4A61-9C3F-4D58-8E17-3F6A52D9C408}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=QuantTrading Studio
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=QuantTradingStudio-{#AppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName} {#AppVersion}（量化交易工作台）
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; 覆盖 Inno 自带的两条（桌面快捷方式 / 附加快捷方式分组标题）
CreateDesktopIcon=创建桌面快捷方式(&D)
AdditionalIcons=附加快捷方式：
; 我们自己加的
MyDataNote=策略、行情导入与账本都在程序目录下：{#MyStrategiesDir}（策略）、{#MyDataDir}（数据）。卸载时默认保留。
MyDocsIcon=安装与数据说明

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Dirs]
; 用户数据目录：安装时就建好，并放开 Users 修改权限 —— 「为所有用户安装」到 Program Files 时，
; 普通用户（非管理员）也能直接往 strategies\ 复制策略文件、往 data\ 里放 CSV；
; uninsneveruninstall：卸载时不删除这些目录（配合 [Code] 里的询问，默认保留用户数据）。
Name: "{#MyStrategiesDir}"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{#MyDataDir}"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{#MyDataDir}\csv"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{#MyDataDir}\level2"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{#MyDataDir}\cache"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
; 程序文件：**排除** data\ 与 strategies\（用户数据目录由 [Dirs] 创建；
; 打包目录里若残留了自检跑出来的数据，绝不能跟着安装包发出去）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "data\*,strategies\*"
Source: "{#DataNoteFile}"; DestDir: "{app}"; Flags: ignoreversion
; 内置示例策略 / 模板 → {app}\_seed\（程序首次启动播种到 strategies\；_seed 是程序文件，
; 卸载时跟着走，而 strategies\ 是用户数据，卸载默认保留）
Source: "{#SeedSourceDir}\*.py"; DestDir: "{app}\_seed"; Excludes: "__init__.py"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{group}\{cm:MyDocsIcon}"; Filename: "{app}\{#DataNoteFile}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#DataNoteFile}"; Description: "{cm:MyDocsIcon}"; Flags: shellexec postinstall skipifsilent unchecked

[Code]
{ 交互式卸载时问一句是否删除用户数据；静默卸载一律保留（CI 里靠这条断言「卸载不删数据」）。

  v1.5.1 起用户数据在安装目录内，所以「否」（默认）= 把安装目录下的 strategies 与 data 一起留下；
  另：旧版本（≤ v1.5.0）的数据在 %LOCALAPPDATA%\QuantTradingStudio，选「是」时一并清掉。
  注意：本段是 Pascal 注释，里面不能再出现花括号（否则注释会被提前闭合）。 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  StrategiesDir: String;
  LegacyDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{#MyDataDir}');
  StrategiesDir := ExpandConstant('{#MyStrategiesDir}');
  LegacyDir := ExpandConstant('{#MyLegacyDataDir}');
  if (not DirExists(DataDir)) and (not DirExists(StrategiesDir)) and (not DirExists(LegacyDir)) then
    Exit;
  if UninstallSilent then
    Exit;
  if MsgBox('是否同时删除用户数据？' + #13#10 + #13#10 +
            '策略：' + StrategiesDir + #13#10 +
            '数据：' + DataDir + #13#10 + #13#10 +
            '（自选池、自己放进来的策略、行情导入文件、模拟盘账本。选「否」会保留，重装/升级后接着用）',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(DataDir, True, True, True);
    DelTree(StrategiesDir, True, True, True);
    if DirExists(LegacyDir) then
      DelTree(LegacyDir, True, True, True);
  end;
end;
