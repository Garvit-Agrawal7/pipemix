; PipeMix installer. Wraps the PyInstaller one-dir output at {#PipemixDistDir}
; (built by build_win.py) and chains the WebView2 evergreen bootstrapper for
; Win10 machines that lack EdgeChromium. Run through build_win.py, which
; passes MyAppVersion and PipemixDistDir on the ISCC command line -- the
; version has one source of truth, pyproject.toml, and must never be
; hand-typed here.
;
; VB-CABLE is bundled under VB-Audio's donationware terms
; (https://vb-audio.com/Services/licensing.htm): the user must be able to see
; it is VB-Audio's and that they can donate, hence the finish page text.

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

[Messages]
; Set here, not from [Code], so Inno sizes the label before placing the run
; checkboxes under it.
FinishedLabel=Setup has finished installing [name] on your computer. The application may be launched by selecting the installed shortcuts.%n%nPipeMix uses VB-CABLE by VB-Audio (www.vb-cable.com), which is donationware. If it helps you, please donate. Installing it needs admin approval and may need a reboot.

[Files]
Source: "{#PipemixDistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#VBcableDir}\*"; DestDir: "{app}\VB-CABLE"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{group}\Install VB-CABLE"; Filename: "{app}\VB-CABLE\VBCABLE_Setup_x64.exe"; WorkingDir: "{app}\VB-CABLE"

[Run]
; The setup's manifest requires admin, so it needs shellexec to get a UAC
; prompt. Listed first and waited on so the driver is in before PipeMix starts.
Filename: "{app}\VB-CABLE\VBCABLE_Setup_x64.exe"; WorkingDir: "{app}\VB-CABLE"; Description: "Install VB virtual audio driver (recommended, needs admin approval)"; Flags: postinstall shellexec waituntilterminated skipifsilent; Check: not IsVBCableInstalled
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

// Service name from the package's vbMmeCable64_win10.inf. Without this an
// upgrade would offer, ticked, to reinstall a driver that is already there.
// The service key outlives an uninstall of VB-CABLE, so count the live device
// instances under Enum rather than testing that the key exists.
function IsVBCableInstalled: Boolean;
var
  Count: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM, 'SYSTEM\CurrentControlSet\Services\VBAudioVACMME\Enum', 'Count', Count) and (Count > 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not IsWebView2Installed then
    InstallWebView2;
end;
