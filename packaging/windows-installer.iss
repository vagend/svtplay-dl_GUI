; Inno Setup script for the Windows installer (built by .github/workflows/build.yml):
;   iscc /DAppVersion=1.2.2 /DSourceExe="dist\svtplay-dl GUI.exe" packaging\windows-installer.iss
; Installs for the current user only (no admin prompt) into %LOCALAPPDATA%\Programs\svtplay-dl GUI,
; adds a Start-menu entry, and lists the app under Settings > Apps with an Uninstall button.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\dist\svtplay-dl GUI.exe"
#endif

[Setup]
AppId={{6F1C2E5B-0D7A-4B8E-9C3F-5A2D8E1B7C40}
AppName=svtplay-dl GUI
AppVersion={#AppVersion}
AppVerName=svtplay-dl GUI {#AppVersion}
AppPublisher=svtplay-dl GUI
AppPublisherURL=https://github.com/vagend/svtplay-dl_GUI
DefaultDirName={userpf}\svtplay-dl GUI
DefaultGroupName=svtplay-dl GUI
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=svtplay-dl-GUI-windows-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\svtplay-dl GUI.exe
UninstallDisplayName=svtplay-dl GUI
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "svtplay-dl GUI.exe"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\svtplay-dl GUI"; Filename: "{app}\svtplay-dl GUI.exe"
Name: "{userdesktop}\svtplay-dl GUI"; Filename: "{app}\svtplay-dl GUI.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; Flags: unchecked

[Registry]
; leftovers from the early browser-based version: its svtplaydl-gui:// link and its start-at-login entry
Root: HKCU; Subkey: "Software\Classes\svtplaydl-gui"; Flags: deletekey dontcreatekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "svtplay-dl GUI"; Flags: deletevalue dontcreatekey

[Run]
Filename: "{app}\svtplay-dl GUI.exe"; Description: "Open svtplay-dl GUI"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the app's settings, logs and temporary files (svtplay-dl, ffmpeg and your downloads are left alone)
Type: filesandordirs; Name: "{userappdata}\svtplay-dl-gui"
Type: filesandordirs; Name: "{localappdata}\svtplay-dl-gui"
Type: files; Name: "{app}\svtplay-dl GUI.old.exe"
Type: dirifempty; Name: "{app}"
