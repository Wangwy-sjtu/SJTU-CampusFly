#define AppName "SJTU校园飞"
#define AppVersion "1.0.0"
[Setup]
AppId={{960FC7AA-6061-4BFE-8952-9A93E94F6710}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Wangwy-sjtu
AppPublisherURL=https://github.com/Wangwy-sjtu/SJTU-CampusFly
DefaultDirName={localappdata}\Programs\SJTU-CampusFly
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\installer-output
OutputBaseFilename=SJTU-CampusFly-1.0.0-Setup-x64
SetupIconFile=..\assets\campusfly.ico
UninstallDisplayIcon={app}\SJTU校园飞.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Files]
Source: "..\dist\SJTU-CampusFly\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\SJTU校园飞.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\SJTU校园飞.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\SJTU校园飞.exe"; Description: "打开 {#AppName}"; Flags: nowait postinstall skipifsilent
