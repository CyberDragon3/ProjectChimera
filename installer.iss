; Inno Setup 6 script for Project Chimera
; Build with:  iscc installer.iss

#define MyAppName "Project Chimera"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Chimera Labs"
#define MyAppURL "https://chimera.invalid/"
#define MyAppExeName "Chimera.exe"

[Setup]
AppId={{8C5B5E3A-1F7D-4C9B-9A52-CHIMERA000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\Chimera
DefaultGroupName=Chimera
DisableProgramGroupPage=yes
OutputDir=installer-output
OutputBaseFilename=ChimeraSetup
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startup";     Description: "Launch Chimera at first login";                 GroupDescription: "Startup:";          Flags: unchecked

[Files]
Source: "dist\Chimera\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Chimera";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Chimera"; Flags: nowait postinstall skipifsilent

[Code]
function OllamaInstalled(): Boolean;
var
  P1, P2, P3: String;
begin
  P1 := ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe');
  P2 := ExpandConstant('{pf}\Ollama\ollama.exe');
  P3 := ExpandConstant('{pf32}\Ollama\ollama.exe');
  Result := FileExists(P1) or FileExists(P2) or FileExists(P3);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not OllamaInstalled() then
  begin
    MsgBox(
      'Project Chimera needs Ollama to run its Executive layer.' + #13#10 + #13#10 +
      'Ollama was not detected on this machine.' + #13#10 + #13#10 +
      'You can install it from:' + #13#10 +
      '    https://ollama.com/download' + #13#10 + #13#10 +
      'Setup will continue — but Chimera will log Executive errors until ' +
      'Ollama is installed and running.',
      mbInformation, MB_OK);
  end;
end;
