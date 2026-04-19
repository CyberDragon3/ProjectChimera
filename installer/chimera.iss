; Inno Setup 6 script for Chimera.
; Build prereqs:
;   1. pyinstaller installer/chimera.spec --noconfirm
;   2. "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\chimera.iss

#define AppName       "Chimera"
#define AppVersion    "0.1.0"
#define AppPublisher  "Terry"
#define AppExeName    "chimera.exe"

[Setup]
AppId={{9E1C2E7A-7E91-4D51-9E1B-2F3C8C3A4D11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=ChimeraSetup-{#AppVersion}
OutputDir=..\dist\installer
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "logonstartup"; Description: "Start Chimera at user logon (Task Scheduler)"; GroupDescription: "Startup options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\chimera\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\config\chimera.toml"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--tray"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--tray"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--tray"; Description: "Launch Chimera"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; Register / deregister the Task Scheduler entry that launches at logon.
; We invoke schtasks.exe directly (not via cmd /C) to dodge the cmd-quoting
; minefield around an executable path that may contain spaces.
[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Params: string;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if IsTaskSelected('logonstartup') then
    begin
      Params := '/Create /F /SC ONLOGON /RL HIGHEST /TN "Chimera" /TR "\"' +
                ExpandConstant('{app}\{#AppExeName}') + '\" --tray"';
      if not Exec(ExpandConstant('{sys}\schtasks.exe'), Params, '',
                  SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      begin
        MsgBox('Could not register Chimera as a logon task (schtasks code ' +
               IntToStr(ResultCode) + '). You can run scripts\install_task.ps1 manually.',
               mbInformation, MB_OK);
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "Chimera"', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
