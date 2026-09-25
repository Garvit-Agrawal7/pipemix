; PipeMix installer. Wraps the PyInstaller one-dir output at {#PipemixDistDir}
; (built by build_win.py) and chains the WebView2 evergreen bootstrapper for
; Win10 machines that lack EdgeChromium. Run through build_win.py, which
; passes MyAppVersion and PipemixDistDir on the ISCC command line -- the
; version has one source of truth, pyproject.toml, and must never be
; hand-typed here.
;
; VB-CABLE is not bundled: VB-Audio's licence forbids redistribution without
; a written agreement. PipeMix runs in mirror mode without it and says so at
; first launch, so the finish page just points at the download instead.

#ifndef MyAppVersion
  #error MyAppVersion must be defined -- build via build_win.py, or pass /DMyAppVersion=x.y.z to ISCC directly
#endif
#ifndef PipemixIconFile
  #error PipemixIconFile must be defined -- build via build_win.py, or pass /DPipemixIconFile=<path to data\icons\pipemix.ico> to ISCC directly
#endif

#ifndef PipemixDistDir
  #error PipemixDistDir must be defined -- build via build_win.py, or pass /DPipemixDistDir=<path to the PyInstaller onedir output> to ISCC directly
#endif

#define MyAppName "PipeMix"
#define MyAppPublisher "PipeMix"
#define MyAppExeName "pipemix.exe"
#define MyAppURL "https://github.com/gaurav-066/pipemix"

[Setup]
AppId={{61C3E08F-3B07-4405-AC3C-D740ABE137B0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
; Per-user install, no admin prompt -- nothing PipeMix does needs machine scope.
DefaultDirName={localappdata}\Programs\PipeMix
DefaultGroupName=PipeMix
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=pipemix-setup
OutputDir=.
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; The installer's own icon, and what Add/Remove Programs shows. Passed in by
; build_win.py rather than reached for inside the dist dir, because
; PyInstaller buries bundled data under _internal/ and that path is its
; business, not the installer's.
SetupIconFile={#PipemixIconFile}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#PipemixDistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// The evergreen bootstrapper is ~2 MB and Microsoft's own permalink for it,
// so it is fetched at install time rather than carried in the installer --
// carrying it would just mean shipping a stale copy.
const
  WebView2BootstrapperURL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';
  WebView2ClientGuid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

// DownloadTemporaryFile needs Inno Setup 6.4+ (it is the version this
// script is written against); older ISCC.exe will fail to compile this
// file with an "unknown identifier" error, which is the honest failure --
// nothing here silently no-ops on an old toolchain.
procedure InstallWebView2;
var
  BootstrapperPath: String;
  ResultCode: Integer;
begin
  try
    DownloadTemporaryFile(WebView2BootstrapperURL, 'MicrosoftEdgeWebView2Setup.exe', '', nil);
  except
    // No network, or Microsoft's endpoint is down -- do not fail the whole
    // install over an optional runtime the app can still prompt for later.
    Log('WebView2 bootstrapper download failed: ' + GetExceptionMessage);
    Exit;
  end;
  BootstrapperPath := ExpandConstant('{tmp}\MicrosoftEdgeWebView2Setup.exe');
  Exec(BootstrapperPath, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not IsWebView2Installed then
    InstallWebView2;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10 + #13#10 +
      'PipeMix runs in mirror mode until VB-CABLE is installed, with a banner ' +
      'saying so. For fully synced multi-output audio, get the free driver from ' +
      'https://vb-audio.com/Cable/ and relaunch PipeMix.';
end;
