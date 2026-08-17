; ============================================================
;  installer.iss - oimimo Inno Setup script
;  Compile: called by build.bat via ISCC.exe, or open in Inno Setup IDE
;  Prereq: run PyInstaller first to produce ..\dist\oimimo\
; ============================================================

#define MyAppName "oimimo"
#define MyAppDisplayName "oimimo scheduler"
#ifndef MyAppVersion
  #define MyAppVersion "1.4.0"
#endif
#define MyAppPublisher "oimimo"
#define MyAppExeName "oimimo.exe"
#define DistDir "..\dist\oimimo"

[Setup]
; AppId must never change: overwrite-install is recognized as the same app
AppId={{8F2A1C64-5B7E-4D29-9C3A-0E6D8B4F17A2}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; user-level install: no admin required, database stays writable
PrivilegesRequired=lowest
DefaultDirName={localappdata}\{#MyAppName}
DisableProgramGroupPage=yes
Uninstallable=yes
OutputDir=..\dist
OutputBaseFilename=oimimo-setup
SetupIconFile=..\static\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; package PyInstaller output; exclude runtime files created during local testing
; so the installer never ships a database and never overwrites user data on upgrade
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "orders.db,*.db,*.db.bak_*,logs\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppDisplayName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; uninstall only removes program cache; orders.db (user data) is kept on purpose
Type: filesandordirs; Name: "{app}\__pycache__"