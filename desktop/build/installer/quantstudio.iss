; QuantTrading Studio —— Windows 安装包脚本（Inno Setup 6+）
;
; 编译（本地 / CI 都一样；路径都从本文件所在目录算，所以参数用相对路径即可）：
;   ISCC.exe /DAppVersion=1.5.0 ^
;            /DSourceDir=..\output\dist\QuantTradingStudio ^
;            /DOutputDir=..\output\artifacts ^
;            quantstudio.iss
;
; 可用 /D 覆盖的宏：AppVersion / SourceDir / OutputDir / AppIcon / DataNoteFile
;
; 设计要点（改这个文件前先读）：
;   * 默认**按当前用户安装**（PrivilegesRequired=lowest）→ 不弹 UAC，装到
;     %LOCALAPPDATA%\Programs\QuantTrading Studio；向导里仍可切换「为所有用户安装」（会自动提权）；
;   * 开始菜单快捷方式 + 可选桌面快捷方式 + 「应用和卸载」里的卸载项（Inno 自动登记，含图标/版本/发布者）；
;   * **卸载默认不动用户数据**：自选池 / 策略 / 模拟盘账本都在 %LOCALAPPDATA%\QuantTradingStudio，
;     交互式卸载会多问一句「是否同时删除用户数据」，默认「否」；静默卸载永不删数据；
;   * 安装/卸载前会自动关闭正在运行的 QuantTrading Studio（Inno 的 CloseApplications）；
;   * 升级：AppId 固定 + UsePreviousAppDir，覆盖安装会沿用上次的安装目录；
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

#define MyAppName "QuantTrading Studio"
#define MyAppExe "QuantTradingStudio.exe"
#define MyAppURL "https://github.com/AS2009/quant-trading-studio"
#define MyDataDir "{localappdata}\QuantTradingStudio"

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
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
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
MyDataNote=自选池、策略、模拟盘账本保存在 {#MyDataDir}（卸载时默认保留）。
MyDocsIcon=安装与数据说明

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DataNoteFile}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{group}\{cm:MyDocsIcon}"; Filename: "{app}\{#DataNoteFile}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#DataNoteFile}"; Description: "{cm:MyDocsIcon}"; Flags: shellexec postinstall skipifsilent unchecked

[Code]
{ 交互式卸载时问一句是否删除用户数据；静默卸载一律保留（CI 里靠这条断言「卸载不删数据」） }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{#MyDataDir}');
  if not DirExists(DataDir) then
    Exit;
  if UninstallSilent then
    Exit;
  if MsgBox('是否同时删除用户数据？' + #13#10 + #13#10 + DataDir + #13#10 +
            '（自选池、策略、模拟盘账本。选「否」会保留，方便重装后继续用）',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
