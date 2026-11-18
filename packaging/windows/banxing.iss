; Inno Setup 6 script for the Windows installer of Banxing (伴行).
;
; This file is UTF-8 WITH a byte-order mark: the compiler reads a script without one
; in the system code page, and the Chinese shortcut name would be mangled
; (tests/test_repo.py checks the mark is still there).
;
; Built by packaging/windows/build.ps1, which passes:
;   /DAppVersion=0.4.0a2  /DNumericVersion=0.4.0.2  /DSourceDir=<the PyInstaller folder>
;   /DOutputDir=<where the setup program goes>  /DIconFile=<banxing.ico>
;
; What the installer does, and nothing more:
;   - per user, no administrator rights: files go to %LOCALAPPDATA%\Programs\Banxing;
;   - one Start Menu shortcut, a desktop shortcut if ticked;
;   - an entry under Settings > Apps (HKCU) with its uninstaller;
;   - it never touches %APPDATA%\Banxing — the user's settings, protected API key and a
;     ledger they chose to keep stay through an upgrade AND through uninstalling
;     (the app's own menu "Clear local data" removes them);
;   - the program is not code-signed.
;
; STATUS: checked on a Windows CI runner only (silent install / run / reinstall /
; uninstall, packaging/windows/verify-installer.ps1). The wizard pages have not been
; clicked through by a person on a real desktop.

#ifndef AppVersion
  #error AppVersion is not defined (use packaging/windows/build.ps1)
#endif
#ifndef NumericVersion
  #error NumericVersion is not defined
#endif
#ifndef SourceDir
  #error SourceDir is not defined
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
; Never change AppId: it is how a later version finds and replaces this one.
AppId={{53BFD128-3CC7-4A13-8915-94F7446099F3}
AppName=伴行 Banxing
AppVersion={#AppVersion}
AppVerName=伴行 Banxing {#AppVersion}
AppPublisher=QShin21
AppPublisherURL=https://github.com/QShin21/collaborator
AppSupportURL=https://github.com/QShin21/collaborator/issues
VersionInfoVersion={#NumericVersion}
VersionInfoDescription=Banxing setup
PrivilegesRequired=lowest
DefaultDirName={autopf}\Banxing
DisableProgramGroupPage=yes
DisableWelcomePage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The running app holds this mutex (banxing/cli.py): setup and uninstall ask to close it first.
AppMutex=Banxing-Banxing
UninstallDisplayName=伴行 Banxing
UninstallDisplayIcon={app}\Banxing.exe
OutputDir={#OutputDir}
OutputBaseFilename=Banxing-v{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef IconFile
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\伴行 Banxing"; Filename: "{app}\Banxing.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\伴行 Banxing"; Filename: "{app}\Banxing.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Banxing.exe"; Description: "{cm:LaunchProgram,Banxing}"; Flags: nowait postinstall skipifsilent
