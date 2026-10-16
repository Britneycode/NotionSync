; Notion 同步 安装包（Inno Setup 6）
;
; 编译：
;   & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
; 前置：先跑 pyinstaller 出 dist\NotionSync\（见 RUNBOOK「构建」）
; 产物：dist\installer\NotionSync-Setup-<版本>.exe
;
; 向导界面是英文：Inno 6.7.3 自带的 Languages\ 里没有简体中文。要中文向导得从
; 官方翻译页下载 ChineseSimplified.isl 放进该目录，再在下面加一段 [Languages]。
;
; 默认装到当前用户目录（不弹 UAC）；用户在向导首页可以改成给所有用户装。

#define AppName "Notion 同步"
#define AppExeName "NotionSync.exe"
#define AppVersion "2.1.0"  ; 发版时与 apps/desktop/pyproject.toml 的 version 一起改

[Setup]
AppId={{415B11D6-CD93-4509-BF85-D1323CEFF4E2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\NotionSync
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=NotionSync-Setup-{#AppVersion}
SetupIconFile=..\apps\desktop\src\notion_sync_desktop\resources\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
; 两个都默认不勾（Inno 的 Tasks 默认是勾上的，要 `unchecked` 才是"可选"）：
; 桌面快捷方式多一个图标，开机自启会写注册表，都不该在用户没表态时替用户决定。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（登录后待在托盘里，不弹窗）"; GroupDescription: "启动选项："; Flags: unchecked

[Files]
; dist\NotionSync\ 是 PyInstaller 的 onedir 产物：三个 exe 加一个 _internal\
Source: "..\dist\NotionSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启走 HKCU 的 Run 键：装在用户目录，不需要管理员；卸载时删掉这个值。
; `--tray` 是必须的——不带它每次登录都会弹一个窗口出来（见 __main__.start_hidden）。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NotionSync"; ValueData: """{app}\{#AppExeName}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart
; 设置页里的「开机自动启动」开关写的是同一条值，安装时没勾、后来在界面里打开也算数，
; 所以卸载要无条件删一次。ValueType: none = 只登记"卸载时删掉"，安装时什么都不建。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "NotionSync"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
