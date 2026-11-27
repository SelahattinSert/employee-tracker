; Inno Setup script for the Monitor Agent GUI installer.
;
; A standard Windows wizard: welcome, a page for the server address and API token, a page
; with the employee-notice confirmation, progress, and finish. It installs under Program
; Files and runs gui-install.ps1 to create the virtual environment, write an owner-only
; configuration and register the scheduled tasks. No secret is embedded in the installer;
; the server address and token are entered in the wizard.
;
; Compile on Windows with:  iscc /DMyAppVersion=2.0.0 installer.iss
; The compiler expects the payload (wheel, requirements.lock, gui-install.ps1,
; gui-uninstall.ps1, and optionally ca.crt) in a "payload" folder next to this script.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{9E2C8B74-6E1A-4E2E-9E3A-4D7C2F5A1B20}
AppName=Monitor Agent
AppVersion={#MyAppVersion}
AppPublisher=Monitor Agent
DefaultDirName={autopf}\MonitorAgent
DefaultGroupName=Monitor Agent
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayName=Monitor Agent
OutputBaseFilename=MonitorAgent-Windows-{#MyAppVersion}-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "payload\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\gui-uninstall.ps1"" -InstallDir ""{app}"""; \
  RunOnceId: "RemoveMonitorAgentTasks"; Flags: runhidden

[Code]
var
  ConnectionPage: TInputQueryWizardPage;
  NoticePage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  ConnectionPage := CreateInputQueryPage(wpSelectDir,
    'Server connection',
    'Where should this computer report to?',
    'Enter the Monitor Server address and the agent API token from the admin panel. ' +
    'The token is stored on this computer and never shown again.');
  ConnectionPage.Add('Server address (for example https://host:8443):', False);
  ConnectionPage.Add('Agent API token:', True);

  NoticePage := CreateInputOptionPage(ConnectionPage.ID,
    'Employee notice',
    'Sensitive telemetry',
    'Screenshots, active-window titles and file audit are sensitive. Enable them only ' +
    'after employees have been notified in line with local law and your policy. ' +
    'Leaving this unchecked installs with those collectors disabled.',
    False, False);
  NoticePage.Add('Employees have been notified; enable screenshots, active window and file audit.');
  NoticePage.Values[0] := True;
end;

function TrimText(const Value: string): string;
begin
  Result := Trim(Value);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Server: string;
  Token: string;
begin
  Result := True;
  if CurPageID = ConnectionPage.ID then
  begin
    Server := TrimText(ConnectionPage.Values[0]);
    Token := TrimText(ConnectionPage.Values[1]);
    if Server = '' then
    begin
      MsgBox('Enter the server address.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if (Pos('://', Server) > 0) and (Lowercase(Copy(Server, 1, 8)) <> 'https://') then
    begin
      MsgBox('The server address must use HTTPS.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Token = '' then
    begin
      MsgBox('Enter the agent API token.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  TokenFile: string;
  Arguments: string;
  ResultCode: Integer;
  CaPath: string;
begin
  if CurStep <> ssPostInstall then
    Exit;

  TokenFile := ExpandConstant('{tmp}\monitor-agent-token.txt');
  if not SaveStringToFile(TokenFile, TrimText(ConnectionPage.Values[1]), False) then
    RaiseException('Could not stage the API token.');

  Arguments :=
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\gui-install.ps1') + '"' +
    ' -InstallDir "' + ExpandConstant('{app}') + '"' +
    ' -ServerUrl "' + TrimText(ConnectionPage.Values[0]) + '"' +
    ' -TokenFile "' + TokenFile + '"';
  CaPath := ExpandConstant('{app}\ca.crt');
  if FileExists(CaPath) then
    Arguments := Arguments + ' -CaFile "' + CaPath + '"';
  if NoticePage.Values[0] then
    Arguments := Arguments + ' -EmployeesNotified';

  try
    if not Exec('powershell.exe', Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('Could not start the configuration step.');
    if ResultCode <> 0 then
      RaiseException('Configuration failed (code ' + IntToStr(ResultCode) +
        '). See ' + ExpandConstant('{app}') + '\install-log.txt.');
  finally
    DeleteFile(TokenFile);
  end;
end;
