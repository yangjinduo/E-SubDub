; E-SubDub Windows 首装包脚本（Inno Setup 6/7，Inno 向导包装 Velopack）
; ====================================================================
; 由 build/build_windows.ps1 调用 ISCC 编译，可传预定义变量：
;   ISCC /DAppVersion=0.2.0 ^
;        /DVeloSetupPath=build\dist\update-feed\yangjinduo.E-SubDub-win-Setup.exe ^
;        /DEulaRtfPath=build\work\EULA-zh.rtf ^
;        installer_velopack.iss
;
; 混合方案（替代旧版 Inno 直装 onedir 的 installer.iss）：
;   1. 保留向导的磁盘空间检测 / 分区选择 / 每用户安装能力：
;      默认选剩余空间最大的固定分区（InitializeWizard /
;      GetLargestFreeDrive），目标分区剩余 < 18 GB 阻止安装
;      （NextButtonClick），目录页常驻剩余空间实时提示行；
;   2. [Files] 把 vpk pack 产出的 Velopack Setup.exe 内嵌到 {tmp}，
;      [Run] 以 --installto "{app}" --silent 静默落盘到用户所选目录
;      （实际布局：{app}\current\ESubDub.exe + {app}\Update.exe）；
;   3. Uninstallable=no：卸载不由 Inno 管理（升级由应用内
;      Velopack 更新完成；卸载即删除安装根目录，models 等数据
;      随目录整体删除）；
;   4. 快捷方式指向 current\ESubDub.exe：current 目录名跨 Velopack
;      更新稳定（更新时整体替换其内容），快捷方式始终有效。
;
; 注意：[Code] 段是 Pascal 语法，注释只能用 //（分号注释会报
; "'BEGIN' expected" 编译错误）。
; ====================================================================

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

#ifndef VeloSetupPath
#define VeloSetupPath "..\dist\update-feed\yangjinduo.E-SubDub-win-Setup.exe"
#endif

; 安装 + 应用内部署完成共需约 16 GB（模型 ~12.7 + 双推理虚拟
; 环境 ~2.7 + 程序 ~0.8）；再留约 2 GB 运行余量，目标分区剩余
; 低于 18 GB 时阻止安装
#define RequiredSpaceGB 18

[Setup]
AppId={{5A1F0E2B-3C4D-4E5F-8A9B-6C7D8E9F0A1B}
AppName=E-SubDub
AppVersion={#AppVersion}
AppPublisher=E-SubDub
; 默认目录经 {code:} 脚本化（见 GetDefaultDir）：剩余空间最大的固定
; 分区 + \ESubDub。不得在 InitializeWizard 里覆盖 WizardForm.DirEdit
; ——那会把静默安装的 /DIR= 命令行参数一并覆盖掉
DefaultDirName={code:GetDefaultDir}
UsePreviousAppDir=no
DefaultGroupName=E-SubDub
DisableProgramGroupPage=yes
; 跳过欢迎页：双击安装后直接进入目录选择页
DisableWelcomePage=yes
; 每用户安装：写 HKCU、无 UAC 弹窗、{autoprograms}/{autodesktop}
; 均落在当前用户目录
PrivilegesRequired=lowest
; 卸载不由 Inno 管理：升级走应用内 Velopack 更新，卸载即删除
; 安装根目录（含 models 等数据）
Uninstallable=no
OutputDir=dist
OutputBaseFilename=ESubDub-Setup-Win64-{#AppVersion}
SetupIconFile=..\app\resources\logo\E-SubDub.ico
WizardStyle=modern
ArchitecturesAllowed=x64compatible
Compression=lzma2/max
SolidCompression=yes
; 安装向导许可协议页：用户必须点"我接受"才能继续（EULA-zh/en 已随
; onedir 落盘 {app}\current\ 供装后查阅，Inno 页面只放中文版向导语言
; 一致；/SILENT、/VERYSILENT 与 --installto 静默安装下本页自动视为
; 已接受）。
; 依《民法典》第 496 条，免除/减轻己方责任、加重对方责任、限制对方
; 主要权利的条款（第三条使用限制、第五条免责声明）须以加粗等合理方
; 式提示——纯 txt 无格式能力，故许可页改用 build_windows.ps1 构建期
; 从 EULA-zh.txt 自动生成的 RTF（加粗重要提示与第三/五条全文），经
; /DEulaRtfPath 传入；独立编译本脚本未传该参数时回退 legal\ 纯文本
#ifndef EulaRtfPath
#define EulaRtfPath "..\legal\EULA-zh.txt"
#endif
LicenseFile={#EulaRtfPath}

[Languages]
; 语言文件随仓库自带（build\innolang\）：CI 上 choco 安装的 Inno Setup
; 不含 ChineseSimplified.isl，不能依赖 "compiler:Languages\..." 路径
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\innolang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
; vpk pack 产出的 Velopack Setup.exe（安装器本体，仅数 MB——模型等
; 大件由应用内下载器完成），内嵌 {tmp} 静默执行后删除
Source: "{#VeloSetupPath}"; DestDir: "{tmp}"; DestName: "ESubDub-Velopack-Setup.exe"; Flags: deleteafterinstall

[Icons]
; current 目录名跨 Velopack 更新稳定：快捷方式始终指向有效启动桩
Name: "{autoprograms}\E-SubDub"; Filename: "{app}\current\ESubDub.exe"
Name: "{autodesktop}\E-SubDub"; Filename: "{app}\current\ESubDub.exe"; Tasks: desktopicon

[Run]
; Velopack 静默安装到用户所选目录：产出 {app}\current\（onedir 程序）
; 与 {app}\Update.exe（更新器），全程无界面
Filename: "{tmp}\ESubDub-Velopack-Setup.exe"; Parameters: "--installto ""{app}"" --silent"; Flags: waituntilterminated; StatusMsg: "正在安装 E-SubDub…"
Filename: "{app}\current\ESubDub.exe"; Description: "启动 E-SubDub"; Flags: nowait postinstall skipifsilent

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
// 默认安装目录（{code:GetDefaultDir}）：剩余空间最大的固定分区 +
// \ESubDub；用户可在目录页自由修改，静默安装时 /DIR= 参数优先
// --------------------------------------------------------------------

function GetDefaultDir(Param: String): String;
begin
  Result := GetLargestFreeDrive() + 'ESubDub\';
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
    Format('目标分区剩余空间：%.1f GB（安装 + 部署完成后共需约 16 GB）', [G]);
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
  // 注意：不在此处覆盖 WizardForm.DirEdit.Text——DefaultDirName 已由
  // {code:GetDefaultDir} 提供最大分区默认值，此处覆盖会把静默安装的
  // /DIR= 命令行参数一并吞掉

  // [Files] 只内嵌数 MB 的 Velopack Setup.exe，官方磁盘占用标签
  // （仅统计 [Files]）失去意义，隐藏之
  WizardForm.DiskSpaceLabel.Visible := False;

  // 目录选择页：剩余空间实时提示行（占用官方磁盘标签的位置）
  SpaceLabel := TLabel.Create(WizardForm);
  SpaceLabel.Parent := WizardForm.SelectDirPage;
  SpaceLabel.Left := WizardForm.DiskSpaceLabel.Left;
  SpaceLabel.Top := WizardForm.DiskSpaceLabel.Top;
  SpaceLabel.Width := WizardForm.SelectDirPage.Width;
  SpaceLabel.Font.Style := [fsBold];
  SpaceLabel.Caption := '';
  WizardForm.DirEdit.OnChange := @DirEditChange;
  UpdateSpaceLabel();
end;

// --------------------------------------------------------------------
// < 18 GB 阻止安装
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
        Format('目标分区剩余空间仅 %.1f GB，不足所需的 18 GB。'#13#10#13#10 +
          '请先清理该分区，或返回上一步选择剩余空间充足的其他分区。', [G]),
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;
