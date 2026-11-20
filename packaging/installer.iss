; Inno Setup 6 script for the LBR Dashboard Windows installer (issue #53).
;
; Do not compile this file by hand: scripts/build_installer.py extracts the
; tested portable ZIP and passes every value below with /D, so the installer
; always wraps the same files as the ZIP and takes its version from
; src/lbr_dashboard/version.py. See docs/windows-installer.md.
;
; Per-user by default: no administrator rights, installs to
; %LOCALAPPDATA%\Programs\LBR Dashboard. Choosing "all users" in the first
; dialog (or /ALLUSERS) asks for elevation and installs to Program Files.

#if VER < EncodeVer(6, 3, 0)
  #error Inno Setup 6.3 or newer is required
#endif
#ifndef AppVersion
  #error AppVersion is not defined; build with scripts/build_installer.py
#endif
#ifndef FileVersion
  #error FileVersion is not defined; build with scripts/build_installer.py
#endif
#ifndef SourceDir
  #error SourceDir is not defined; build with scripts/build_installer.py
#endif
#ifndef OutputDir
  #error OutputDir is not defined; build with scripts/build_installer.py
#endif
#ifndef OutputBaseName
  #error OutputBaseName is not defined; build with scripts/build_installer.py
#endif
#ifndef Compression
  #define Compression "lzma2/max"
#endif

#define AppName "LBR Dashboard"
#define AppExe "LBRDashboard.exe"
#define AppPublisher "Longhorn Baja Racing"
#define AppURL "https://github.com/longhorn-baja-racing/dashboard"
; Per-user data the dashboard writes under %LOCALAPPDATA%. The installer never
; puts files there, and the uninstaller removes it only when the user says so.
; tests/test_windows_installer.py checks these match the application code.
#define LogsFolder "LBR Dashboard"
#define PrefsFolder "LonghornBaja\dashboard"
#define PrefsParent "LonghornBaja"

[Setup]
; Never change AppId: Windows and Inno Setup use it to find an installed copy,
; so a new version upgrades it in place instead of installing a second one.
AppId={{7CB5E302-B161-4290-974C-56AD24646B70}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=Copyright (c) 2026 {#AppPublisher}. MIT License.
VersionInfoVersion={#FileVersion}
VersionInfoProductVersion={#FileVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
Compression={#Compression}
SolidCompression=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.RemoveUserData=Also delete your LBR Dashboard logs and preferences?%n%n%1%n%2%n%nYour telemetry files are not affected. Choose No to keep these for a later install.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Upgrades and downgrades start from a clean program folder, so no library
; from another version is left next to the new one.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\notices"
Type: filesandordirs; Name: "{app}\samples"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only program files live here; user data is under %LOCALAPPDATA% (see [Code]).
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Logs, Prefs: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Logs := ExpandConstant('{localappdata}\{#LogsFolder}');
  Prefs := ExpandConstant('{localappdata}\{#PrefsFolder}');
  { A silent uninstall always keeps user data; so does answering No. }
  if UninstallSilent or not (DirExists(Logs) or DirExists(Prefs)) then
    Exit;
  if SuppressibleMsgBox(FmtMessage(CustomMessage('RemoveUserData'), [Logs, Prefs]),
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    DelTree(Logs, True, True, True);
    DelTree(Prefs, True, True, True);
    RemoveDir(ExpandConstant('{localappdata}\{#PrefsParent}'));
  end;
end;
