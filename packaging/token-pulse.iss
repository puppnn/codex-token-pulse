#ifndef AppVersion
  #define AppVersion "1.0.2"
#endif

#define AppName "Token Pulse"
#define AppPublisher "puppnn"
#define AppURL "https://github.com/puppnn/codex-token-pulse"

[Setup]
AppId={{DAD0283F-50D9-4EA4-B39C-970B71D5B3FD}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\Token Pulse
DefaultGroupName=Token Pulse
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=CodexTokenPulse-Setup-v{#AppVersion}
SetupIconFile=..\assets\token-pulse.ico
UninstallDisplayIcon={app}\TokenPulse.exe
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked
Name: "startup"; Description: "登录 Windows 后自动启动"; GroupDescription: "启动选项："; Flags: unchecked

[Files]
Source: "..\dist\TokenPulse.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\TokenPulseExporter.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\TokenPulseAnalytics.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Token Pulse"; Filename: "{app}\TokenPulse.exe"
Name: "{group}\Token Pulse Analytics"; Filename: "{app}\TokenPulseAnalytics.exe"; Parameters: "--open"
Name: "{autodesktop}\Token Pulse"; Filename: "{app}\TokenPulse.exe"; Tasks: desktopicon
Name: "{userstartup}\Token Pulse"; Filename: "{app}\TokenPulse.exe"; Tasks: startup

[Run]
Filename: "{app}\TokenPulseAnalytics.exe"; Parameters: "--open"; Description: "Open Token Pulse Analytics"; Flags: nowait postinstall skipifsilent unchecked
Filename: "{app}\TokenPulse.exe"; Description: "启动 Token Pulse"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\TokenPulse.exe"
Type: files; Name: "{app}\TokenPulseExporter.exe"
Type: files; Name: "{app}\TokenPulseAnalytics.exe"
Type: dirifempty; Name: "{app}"

[Code]
procedure StopTokenPulseProcess(const ImageName: String);
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM ' + ImageName,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopTokenPulseProcess('TokenPulse.exe');
    StopTokenPulseProcess('TokenPulseExporter.exe');
    StopTokenPulseProcess('TokenPulseAnalytics.exe');
  end;
end;
