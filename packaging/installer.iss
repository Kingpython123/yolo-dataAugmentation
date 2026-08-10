; Inno Setup 脚本: 生成 Windows 安装包。
;
; 刻意不预设工作区: 产物可达数十 GB, 应由用户在首次启动时选一块大容量磁盘,
; 而不是默认写到安装目录(Program Files 对普通用户还不可写)。
;
; 版本号由 build.ps1 用 /DMyAppVersion=x.y.z 注入, 单一来源是 app/version.py。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "缺陷合成流水线"
#define MyAppShortName "DefectSynth"
#define MyAppPublisher "yolo-dataAugmentation"
#define MyAppExeName "DefectSynth.exe"

[Setup]
AppId={{8C2F4A16-5E3D-4B71-9A0C-6D5F2E7B1C43}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 目标是 64 位 Windows; 32 位系统跑不了打包进去的 numpy/opencv
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 允许非管理员安装到用户目录, 免得为了装个内部工具去找管理员
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"

[Files]
Source: "..\dist\DefectSynth\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清安装目录里的运行时残留。工作区在用户自己选的位置, 里面是生成产物,
; 卸载程序绝不能去动它。
Type: filesandordirs; Name: "{app}\__pycache__"

[Messages]
chinesesimplified.WelcomeLabel2=即将安装 [name/ver]。%n%n首次启动时需要选择一个工作区目录, 用于存放配置、缺陷库与生成产物。产物体积可能达到数十 GB, 请准备一块空间充足的磁盘。%n%n缺陷库作为独立数据包 catalog-data.zip 分发, 安装完成后在程序的「缺陷库」页导入。
