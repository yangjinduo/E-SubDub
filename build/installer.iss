; E-SubDub Windows 安装脚本（Inno Setup 6/7）
; ====================================================================
; 由 build/build_windows.ps1 调用 ISCC 编译，可传预定义变量：
;   ISCC /DAppVersion=0.1.0 /DDistDir=build\dist\ESubDub installer.iss
;
; 需求实现：
;   1. 每用户安装（无需管理员）：PrivilegesRequired=lowest，
;      默认目录 {userpf}\ESubDub（= %LOCALAPPDATA%\Programs）；
;   2. 默认选剩余空间最大的固定分区（覆盖系统盘默认，见
;      InitializeWizard / GetLargestFreeDrive）；
;   3. 目标分区剩余空间实时显示（目录可编辑时跟随刷新）；
;   4. 目标分区剩余 < 25 GB 阻止安装（NextButtonClick）；
;   5. 不显示欢迎页 / 说明页，双击后直接进入目录选择页；"安装+
;      部署完成后共需约 25 GB"由目录选择页常驻提示行承担。
;
; 注意：[Code] 段是 Pascal 语法，注释只能用 //（分号注释会报
; "'BEGIN' expected" 编译错误）。
; ====================================================================

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

#ifndef DistDir
#define DistDir "..\dist\ESubDub"
#endif

; 安装 + 应用内部署（模型约 15 GB + 双推理虚拟环境数 GB）共需的
; 磁盘空间下限（GB），低于此值阻止安装
#define RequiredSpaceGB 25

[Setup]
AppId={{9B6D4A2C-1F37-4E5A-9C42-8A1B5D0E3F71}
AppName=E-SubDub
AppVersion={#AppVersion}
AppPublisher=E-SubDub
UninstallDisplayName=E-SubDub
DefaultDirName={userpf}\ESubDub
DefaultGroupName=E-SubDub
DisableProgramGroupPage=yes
; 跳过欢迎页：双击安装后直接进入目录选择页
DisableWelcomePage=yes
; 每用户安装：写 HKCU、无 UAC 弹窗、{autoprograms}/{autodesktop}
; 均落在当前用户目录
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=ESubDub-Setup-{#AppVersion}
SetupIconFile=..\app\resources\logo\E-SubDub.ico
WizardStyle=modern
WizardResizable=yes
ArchitecturesAllowed=x64compatible
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
; onedir 发行目录整体装入 {app}（主程序 / worker / 内置 ffmpeg /
; 便携 CPython runtime\python / torchaudio 垫片）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\E-SubDub"; Filename: "{app}\ESubDub.exe"
Name: "{autodesktop}\E-SubDub"; Filename: "{app}\ESubDub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ESubDub.exe"; Description: "启动 E-SubDub"; Flags: nowait postinstall skipifsilent

[Code]
const
  DRIVE_FIXED = 3;
  BYTES_PER_GB = 1073741824;

var
  SpaceLabel: TLabel;

// Windows API：按目录查分区空间（三个出参均为 64 位字节数，
// TotalNumberOfFreeBytes 为全分区真实剩余空间）
function GetDiskFreeSpaceExW(
  lpDirectoryName: String;
  var lpFreeBytesAvailableToCaller: Int64;
  var lpTotalNumberOfBytes: Int64;
  var lpTotalNumberOfFreeBytes: Int64): Boolean;
  external 'GetDiskFreeSpaceExW@kernel32.dll stdcall';

function GetDriveTypeW(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

// --------------------------------------------------------------------
// 磁盘工具
// --------------------------------------------------------------------

// 返回剩余空间最大的固定分区根目录（形如 "D:\"），无可用固定分区
// 时回退系统盘 C:\
function GetLargestFreeDrive(): String;
var
  Drive: String;
  FreeAvail: Int64;
  TotalBytes: Int64;
  TotalFree: Int64;
  MaxFree: Int64;
  Best: String;
  I: Integer;
begin
  MaxFree := -1;
  Best := '';
  for I := Ord('C') to Ord('Z') do
  begin
    Drive := Chr(I) + ':\';
    if GetDriveTypeW(Drive) = DRIVE_FIXED then
    begin
      if GetDiskFreeSpaceExW(Drive, FreeAvail, TotalBytes, TotalFree) then
      begin
        if TotalFree > MaxFree then
        begin
          MaxFree := TotalFree;
          Best := Drive;
        end;
      end;
    end;
  end;
  if Best = '' then
    Best := 'C:\';
  Result := Best;
end;

// 目录所在分区剩余空间（GB）；查询失败返回 -1
function FreeSpaceGB(const Dir: String): Double;
var
  Root: String;
  FreeAvail: Int64;
  TotalBytes: Int64;
  TotalFree: Int64;
begin
  Result := -1;
  Root := ExtractFileDrive(Dir);
  if Root <> '' then
  begin
    if Root[Length(Root)] <> '\' then
      Root := Root + '\';
    if GetDiskFreeSpaceExW(Root, FreeAvail, TotalBytes, TotalFree) then
      Result := TotalFree / BYTES_PER_GB;
  end;
end;

// --------------------------------------------------------------------
// 向导页：目录选择页的常驻提示行
// --------------------------------------------------------------------

procedure UpdateSpaceLabel();
var
  G: Double;
begin
  G := FreeSpaceGB(WizardForm.DirEdit.Text);
  if G < 0 then
  begin
    SpaceLabel.Caption := '';
    Exit;
  end;
  SpaceLabel.Caption :=
    Format('目标分区剩余空间：%.1f GB（安装 + 部署完成后共需约 25 GB）', [G]);
  if G < {#RequiredSpaceGB} then
    SpaceLabel.Font.Color := clRed
  else
    SpaceLabel.Font.Color := clGreen;
end;

procedure DirEditChange(Sender: TObject);
begin
  UpdateSpaceLabel();
end;

// --------------------------------------------------------------------
// 初始化
// --------------------------------------------------------------------

procedure InitializeWizard();
begin
  // 默认安装目录：剩余空间最大的固定分区（覆盖 {userpf} 的系统盘默认）；
  // 用户仍可在目录页自由修改
  WizardForm.DirEdit.Text := GetLargestFreeDrive() + 'ESubDub\';

  // 目录选择页：剩余空间实时提示行（放在官方磁盘占用标签上方）
  SpaceLabel := TLabel.Create(WizardForm);
  SpaceLabel.Parent := WizardForm.SelectDirPage;
  SpaceLabel.Left := WizardForm.DiskSpaceLabel.Left;
  SpaceLabel.Top := WizardForm.DiskSpaceLabel.Top - ScaleY(28);
  SpaceLabel.Width := WizardForm.SelectDirPage.Width;
  SpaceLabel.Font.Style := [fsBold];
  SpaceLabel.Caption := '';
  WizardForm.DirEdit.OnChange := @DirEditChange;
  UpdateSpaceLabel();
end;

// --------------------------------------------------------------------
// < 25 GB 阻止安装
// --------------------------------------------------------------------

function NextButtonClick(CurPageID: Integer): Boolean;
var
  G: Double;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    G := FreeSpaceGB(WizardForm.DirEdit.Text);
    if G < 0 then
    begin
      MsgBox('无法读取目标分区的磁盘信息，请确认安装路径有效。', mbError, MB_OK);
      Result := False;
    end
    else if G < {#RequiredSpaceGB} then
    begin
      MsgBox(
        Format('目标分区剩余空间仅 %.1f GB，不足所需的 25 GB。'#13#10#13#10 +
          '请先清理该分区，或返回上一步选择剩余空间充足的其他分区。', [G]),
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;
