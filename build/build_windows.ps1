# E-SubDub 一键构建脚本（Windows）
# ====================================================================
# 产物：
#   build\dist\ESubDub\                        onedir 发行目录
#   build\dist\update-feed\                    Velopack 更新 feed
#     releases.win.json + 全量/增量 nupkg + ESubDub-Setup-<版本>.exe
#     （feed 内首装入口由 Inno 首装包以统一命名提供，取代 vpk 原生的
#      yangjinduo.E-SubDub-win-Setup.exe，见阶段 6.6）
#   build\dist\ESubDub-Setup-<版本>.exe        Inno 首装包
#     （向导式包装 Velopack Setup.exe：磁盘空间检测 / 分区选择沿用，
#      经 --installto 静默落盘到用户所选目录，见 installer_velopack.iss）
#
# 设计要点（方案 C）：主程序用 Nuitka standalone 冻结，但不含任何推理
# 依赖（torch / transformers / demucs / silero-vad 全部交给应用内
# .venv-ml / .venv-tts 子进程环境按需搭建）；两个 worker 脚本与
# 内置 ffmpeg、便携 CPython 以数据文件方式随包分发。
#
# 前置要求（构建机，一次性）：
#   1. Python 3.13 虚拟环境，且已安装（Nuitka 必须钉 4.2.2：4.3 的
#      Python 3.13 frame locals proxy 重写在 PySide6 6.9.3 冻结态触发
#      确定性 access violation——启动日志崩在 clicked.connect 的
#      lambda 上、事件查看器报 pyside6.abi3.dll 0xC0000005 偏移
#      0x183ea，两台不同显卡的测试机均复现；升级 Nuitka 前必须重新
#      做安装包双击启动冒烟测试）：
#        pip install -r build\requirements-freeze.txt nuitka==4.2.2
#   2. .NET SDK + vpk（Velopack 打包 CLI，缺失时本脚本打印安装指引
#      并中止）：
#        winget install Microsoft.DotNet.SDK.8
#        dotnet tool install -g vpk
#   3. Inno Setup 6（必需——唯一安装入口 Inno 首装包及其许可协议页
#      依赖它，缺失即中止构建）；ISCC 依次从 PATH 与标准安装位置探测：
#        winget install -e --id JRSoftware.InnoSetup
#   4. C 编译器：优先 MSVC（VS Build Tools 的 VCTools）；缺失时
#      Nuitka 经 --assume-yes-for-downloads 自动下载 MinGW64 也可用。
#   5. 便携 CPython（python-build-standalone，随安装包分发给最终
#      用户）：提前手动下载解压到 build\runtime\python\；缺失时
#      脚本打印下载链接并中止（不自动下载）
#
# 用法（在项目根、已激活构建 venv 的终端中）：
#   .\build\build_windows.ps1                # 完整构建（onedir + 更新 feed + Inno 首装包）
#   .\build\build_windows.ps1 -SkipInstaller # 只产裸 onedir（跳过 vpk pack 与 Inno 全部步骤）
# ====================================================================

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$BuildDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $BuildDir
Set-Location -Path $ProjectRoot

# ---------- 0. 版本号（唯一真值源：app/core/config.py 的 APP_VERSION） ----------
$ConfigPy = Get-Content -Raw -Path (Join-Path $ProjectRoot 'app\core\config.py')
if ($ConfigPy -notmatch 'APP_VERSION:\s*str\s*=\s*"([^"]+)"') {
    throw "无法从 app\core\config.py 解析 APP_VERSION"
}
$Version = $Matches[1]
Write-Host "== E-SubDub v$Version 构建开始 ==" -ForegroundColor Cyan

# ---------- 1. 前置检查：Nuitka（版本必须钉 4.2.2，见脚本头部注释） ----------
$NuitkaPin = '4.2.2'
$NuitkaVersion = (python -m nuitka --version 2>$null | Select-Object -First 1)
if ([string]::IsNullOrWhiteSpace("$NuitkaVersion")) {
    throw "未检测到 Nuitka。请在构建环境执行：pip install -r build\requirements-freeze.txt nuitka==$NuitkaPin"
}
if ("$NuitkaVersion".Trim() -ne $NuitkaPin) {
    throw "当前 Nuitka 为 $NuitkaVersion，必须钉死 $NuitkaPin（4.3 会造成冻结态安装包双击即崩）。请执行：pip install nuitka==$NuitkaPin"
}

# ---------- 2. 目录准备 ----------
$DistDir = Join-Path $BuildDir 'dist'          # 发行产物（onedir 目录 + 安装包）
$WorkDir = Join-Path $BuildDir 'work'          # Nuitka 中间产物（可随时删除）
$RuntimeStaging = Join-Path $BuildDir 'runtime'  # 便携 CPython 暂存（跨构建复用）
New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir, $RuntimeStaging | Out-Null

# ---------- 3. 便携 CPython（随安装包分发的运行时组件） ----------
# 注意：它不参与编译（Nuitka 用构建环境自身的 Python），而是发行物
# 的一部分——冻结可执行无法 `sys.executable -m venv`，最终用户机器上
# 创建 .venv-ml / .venv-tts 依赖它作基础解释器。需提前手动下载并
# 解压（Win10+ 自带 tar，压缩包内自带顶层 python 文件夹）：
#     tar -xzf cpython-*-install_only_stripped.tar.gz -C build\runtime
# 最终 python.exe 必须位于 build\runtime\python\python.exe。
$PyExe = Join-Path $RuntimeStaging 'python\python.exe'
if (Test-Path $PyExe) {
    Write-Host "== 便携 CPython 已就绪（build\runtime\python）==" -ForegroundColor Green
} else {
    Write-Host @"
未找到便携 CPython（$PyExe）。

请手动下载 python-build-standalone（CPython 3.13.5）并解压到 build\runtime\：

  Windows x86_64（本脚本目标平台）：
  https://github.com/astral-sh/python-build-standalone/releases/download/20250723/cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz

  解压方法（Win10+ 自带 tar，解压后即为 build\runtime\python\）：
  tar -xzf cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz -C build\runtime

  参考（macOS 安装包需在 macOS 机器上构建，Windows 上无法交叉打包）：
  macOS Apple Silicon：
  https://github.com/astral-sh/python-build-standalone/releases/download/20250723/cpython-3.13.5+20250723-aarch64-apple-darwin-install_only_stripped.tar.gz
  macOS Intel：
  https://github.com/astral-sh/python-build-standalone/releases/download/20250723/cpython-3.13.5+20250723-x86_64-apple-darwin-install_only_stripped.tar.gz
"@ -ForegroundColor Yellow
    throw "便携 CPython 未就绪：请按上述链接手动下载解压后重跑构建"
}

# ---------- 4. Nuitka standalone（onedir） ----------
# 只编译主程序与 UI：推理大件全部在应用内子进程环境搭建，安装包
# 目标体积数百 MB（模型 15 GB+ 由应用内下载器完成）。
$MainDist = Join-Path $WorkDir 'main.dist'
if (Test-Path $MainDist) { Remove-Item -Recurse -Force $MainDist }

Write-Host "== Nuitka 编译主程序（standalone / onedir，约 10-40 分钟）==" -ForegroundColor Cyan
$NuitkaArgs = @(
    '--standalone'
    # 自动下载构建工具（ziglang / ccache / dependency walker；本机无
    # MSVC 时自动下载 MinGW64 编译器）
    '--assume-yes-for-downloads'
    "--output-dir=$WorkDir"
    '--output-filename=ESubDub.exe'
    '--enable-plugin=pyside6'
    # Qt 多媒体后端插件（PySide6\plugins\multimedia 的 ffmpeg /
    # windows media 后端 DLL）：QMediaPlayer 试听与视频播放必需，
    # 不在 pyside6 插件的 sensible 默认集内——缺它时 QtMultimedia
    # 模块可导入、播放器可构造，但 play() 无后端静默失败（实测
    # 冻结包各试听点全部无法播放，dist 内 qt-plugins 无 multimedia）
    # 注意选项名全小写：--include-qt-plugins（大写 Qt 会被 Nuitka
    # 直接判为未知选项退出）
    '--include-qt-plugins=sensible,multimedia'
    '--windows-console-mode=disable'
    '--windows-icon-from-ico=app\resources\logo\E-SubDub.ico'
    '--company-name=ESubDub'
    '--product-name=E-SubDub'
    "--file-version=$Version"
    "--product-version=$Version"
    '--file-description=极简字幕与 AI 配音生成工具'
    '--copyright=E-SubDub Contributors'
    # ---- 构建环境可能装有推理大件（如复用开发主环境 venv 打包）：
    #      冻结态主程序运行时绝不 import 它们（推理全部在 .venv-ml /
    #      .venv-tts 子进程），显式排除，防止 Nuitka 静态跟随
    #      hardware.py dev 分支的 import torch 把 2.7 GB torch 编入
    '--nofollow-import-to=torch*'
    '--nofollow-import-to=transformers*'
    '--nofollow-import-to=demucs*'
    '--nofollow-import-to=silero_vad*'
    '--nofollow-import-to=onnxruntime*'
    # ---- worker 脚本：以数据文件随包分发，运行时由各自 venv 的
    #      Python 子进程执行（冻结态 app 包无磁盘源码，不能 import）
    '--include-data-files=app\pipeline\ml_worker.py=app\pipeline\ml_worker.py'
    '--include-data-files=app\pipeline\tts_worker.py=app\pipeline\tts_worker.py'
    # ---- 界面资源（图标 / LOGO）与内置 ffmpeg
    '--include-data-dir=app\resources\icons=app\resources\icons'
    '--include-data-dir=app\resources\logo=app\resources\logo'
    '--include-data-files=libs\windows\ffmpeg.exe=libs\windows\ffmpeg.exe'
    # 注意：torchaudio 垫片不得随包分发！冻结态 .venv-ml / .venv-tts
    # 均安装真包（2.8.0 剥离 C 扩展），应用根目录若存在垫片目录，凡以
    # 应用目录为 CWD 的子进程（python -c 冒烟测试等，sys.path[0]=CWD）
    # 会优先导入垫片（其 resample 依赖 scipy，venv 内未装）→ 误报
    # ImportError 掩盖真实状态（实测见 deploy 日志 2026-10-17）
    # ---- 带包数据的第三方库（CA 证书 / 语言识别模型）
    '--include-package-data=certifi'
    '--include-package-data=langid'
    'app\main.py'
)
python -m nuitka @NuitkaArgs
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka 编译失败（exit $LASTEXITCODE）"
}

# ---------- 5. 组装 onedir 发行目录 ----------
$AppDist = Join-Path $DistDir 'ESubDub'
if (Test-Path $AppDist) { Remove-Item -Recurse -Force $AppDist }
# 用 robocopy /MOVE 而非 Move-Item：刚编译出的 exe 常被杀毒软件
# （Windows Defender 实时扫描等）短暂锁定，目录级 rename 要求内部
# 零占用句柄，会报"访问被拒绝"；robocopy 逐文件复制并自动重试
# （/R:5 /W:3 = 每个文件最多重试 5 次、间隔 3 秒），完成后删源目录
robocopy $MainDist $AppDist /E /MOVE /R:5 /W:3 /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -ge 8) {
    throw "main.dist 移动失败（robocopy exit $LASTEXITCODE）——多为杀毒软件锁定新生成的 exe，建议把项目 build 目录加入 Windows Defender 排除项后重跑"
}

# 开源声明随包分发（页脚「开源声明」入口在冻结态从安装目录按界面语言
# 读取 CN/EN 对应文件；许可证义务：Apache-2.0/MIT 等要求分发时随附
# 声明，PySide6 LGPL 须指明源码获取方式）
Copy-Item -Force `
    -Path (Join-Path $ProjectRoot 'THIRD-PARTY-NOTICES-CN.html'), `
          (Join-Path $ProjectRoot 'THIRD-PARTY-NOTICES-EN.html') `
    -Destination $AppDist

# 用户协议与免责声明随包分发（legal\ 四份 txt：EULA/disclaimer 各中英
# 一份）。放 onedir 根（与 exe 同目录）而非经 Inno [Files] 落 {app}：
# 应用实际运行于 {app}\current\，与 exe 同目录便于应用内「免责声明」
# 入口按 exe 旁路径读取（同「开源声明」的定位逻辑）；且随 Velopack
# 更新整体替换 current\，协议版本自动跟随应用刷新。文件须保持 UTF-8
# 带 BOM（运行期读取依赖），修改时勿另存为 ANSI。安装向导许可页不用
# 这四份 txt——它用 6.4 阶段生成的加粗 RTF（496 条格式条款提示义务）。
Copy-Item -Force `
    -Path (Join-Path $ProjectRoot 'legal\EULA-zh.txt'), `
          (Join-Path $ProjectRoot 'legal\EULA-en.txt'), `
          (Join-Path $ProjectRoot 'legal\DISCLAIMER-zh.txt'), `
          (Join-Path $ProjectRoot 'legal\DISCLAIMER-en.txt') `
    -Destination $AppDist

# 便携 CPython → 发行目录 runtime\python（env_setup 创建 venv 依赖它）；
# Copy-Item 不创建中间目录，先显式建 runtime
New-Item -ItemType Directory -Force -Path (Join-Path $AppDist 'runtime') | Out-Null
Copy-Item -Recurse -Force `
    -Path (Join-Path $RuntimeStaging 'python') `
    -Destination (Join-Path $AppDist 'runtime\python')

Write-Host "== onedir 发行目录就绪：$AppDist ==" -ForegroundColor Green

# ---------- 6. Velopack 更新包（vpk pack）+ Inno 首装包 ----------
# -SkipInstaller：跳过 vpk + Inno 全部步骤，只产裸 onedir（本地
# 冻结态测试布局：exe 直接在发行目录，无 current 父结构）
if ($SkipInstaller) {
    Write-Host "== 已按 -SkipInstaller 跳过 vpk pack 与 Inno 首装包（仅产裸 onedir）==" -ForegroundColor Yellow
} else {
    # ---- 6.1 vpk 定位：优先 dotnet 全局工具默认位置（dotnet tool
    #      install -g vpk 的产物），兜底 PATH；均缺失时中止并给指引 ----
    $VpkExe = Join-Path $env:USERPROFILE '.dotnet\tools\vpk.exe'
    if (-not (Test-Path -LiteralPath $VpkExe)) {
        $VpkCmd = Get-Command vpk -ErrorAction SilentlyContinue
        if ($VpkCmd) { $VpkExe = $VpkCmd.Source } else { $VpkExe = $null }
    }
    if (-not $VpkExe) {
        throw @"
未找到 vpk（Velopack 打包 CLI）。请先安装 .NET SDK 与 vpk（一次性）：
  winget install Microsoft.DotNet.SDK.8
  dotnet tool install -g vpk
"@
    }

    # ---- 6.2 vpk pack：产出更新 feed（releases.win.json + 全量 nupkg +
    #      原生 Setup.exe；--noPortable 不产便携 zip——便携包解压即用、
    #      绕过 Inno 许可协议页，本项目只保留 Inno 首装包一种安装入口）。
    #      增量 delta 需 feed 中已存在
    #      上一版包：本地构建前可手动执行 vpk download github 拉取上一
    #      版；CI 由 release.yml 自动拉取。首版（或空 feed）只有全量包 ----
    $PackId = 'yangjinduo.E-SubDub'
    $UpdateFeedDir = Join-Path $DistDir 'update-feed'
    New-Item -ItemType Directory -Force -Path $UpdateFeedDir | Out-Null
    Write-Host "== vpk pack（Velopack 更新包，channel=win）==" -ForegroundColor Cyan
    $VpkArgs = @(
        'pack',
        '--packId', $PackId,
        '--packVersion', $Version,
        '--packDir', $AppDist,
        '--mainExe', 'ESubDub.exe',
        '--packTitle', 'E-SubDub',
        '--channel', 'win',
        # 不产 Portable.zip：便携包无许可协议页，与"唯一安装入口"冲突
        '--noPortable',
        '--outputDir', $UpdateFeedDir,
        '--icon', (Join-Path $ProjectRoot 'app\resources\logo\E-SubDub.ico')
    )
    # 更新说明：build\release-notes.md 存在时随包内嵌，客户端「检查更新」
    # 弹窗展示的 NotesMarkdown 即来源于此（每次发版前更新该文件）。
    # vpk 会把文件原样内嵌（GitHub Release 正文同样取自它），因此 HTML
    # 注释 <!-- ... --> 作为本地备注在打包前剔除：写入 work 目录的净化
    # 副本再交给 vpk，本地文件中的注释不随包发布
    $ReleaseNotes = Join-Path $BuildDir 'release-notes.md'
    if (Test-Path $ReleaseNotes) {
        $NotesPublish = Join-Path $WorkDir 'release-notes-publish.md'
        $NotesContent = [regex]::Replace((Get-Content -Raw $ReleaseNotes), '(?s)<!--.*?-->', '')
        # 剥离注释后，清掉尾部因此孤立的分隔线（---）与空行
        $NotesContent = $NotesContent.TrimEnd() -replace '(\r?\n\s*)*-{3,}\s*$', ''
        [System.IO.File]::WriteAllText($NotesPublish, $NotesContent)
        $VpkArgs += @('--releaseNotes', $NotesPublish)
    } else {
        Write-Warning "未找到 $ReleaseNotes，本次更新包不含更新说明"
    }
    & $VpkExe @VpkArgs
    if ($LASTEXITCODE -ne 0) {
        throw "vpk pack 失败（exit $LASTEXITCODE）"
    }
    Write-Host "== 更新 feed 就绪：$UpdateFeedDir ==" -ForegroundColor Green

    # ---- 6.3 ISCC 定位：PATH 兜底标准安装位置（含每用户安装） ----
    $IsccExe = $null
    $IsccCmd = Get-Command ISCC -ErrorAction SilentlyContinue
    if ($IsccCmd) {
        $IsccExe = $IsccCmd.Source
    } else {
        foreach ($p in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            'C:\Program Files\Inno Setup 6\ISCC.exe',
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
        )) {
            if (Test-Path -LiteralPath $p) { $IsccExe = $p; break }
        }
    }
    if (-not $IsccExe) {
        throw @"
未找到 ISCC（Inno Setup 6）。本项目唯一安装入口是带许可协议页的 Inno
首装包，ISCC 缺失时中止构建（不再回退 vpk 原生 Setup.exe——它没有
许可协议页，无法履行《民法典》496 条的格式条款提示义务）。请先安装后重跑：
  winget install -e --id JRSoftware.InnoSetup
"@
    } else {
        # ---- 6.4 EULA RTF：构建期从 legal\EULA-zh.txt 生成带加粗的 RTF
        #      交 Inno 许可页渲染（许可页为 RichEdit 控件，识别到 RTF
        #      开头 {\rtf 才按富文本渲染，\b 加粗生效）。依《民法典》
        #      第 496 条，免除/减轻己方责任、加重对方责任、限制对方
        #      主要权利的格式条款（第三条使用限制、第五条免责声明）须
        #      以加粗等合理方式提示，纯 txt 无格式能力。txt 仍是唯一
        #      真值源（随包分发的裸文本形态不变），RTF 为构建中间产物
        #      不进仓库；输出为纯 ASCII（非 ASCII 全部 \u 转义）且不
        #      带 BOM——BOM 会破坏 "{\rtf" 文件头识别 ----
        function ConvertTo-RtfEscape {
            param([string]$Text)
            $sb = [System.Text.StringBuilder]::new()
            foreach ($ch in $Text.ToCharArray()) {
                if ($ch -eq '\' -or $ch -eq '{' -or $ch -eq '}') {
                    [void]$sb.Append('\').Append($ch)
                } elseif ([int]$ch -lt 128) {
                    [void]$sb.Append($ch)
                } else {
                    # \uc1 语义下 '?' 为单字符回退占位（RichEdit 读 \u 时跳过）
                    $code = [int]$ch
                    if ($code -gt 32767) { $code -= 65536 }
                    [void]$sb.Append('\u').Append($code).Append('?')
                }
            }
            $sb.ToString()
        }

        $EulaRtfPath = Join-Path $WorkDir 'EULA-zh.rtf'
        $EulaLines = ([System.IO.File]::ReadAllText((Join-Path $ProjectRoot 'legal\EULA-zh.txt')) -replace "`r`n", "`n").Trim() -split "`n"
        $EulaRtfSb = [System.Text.StringBuilder]::new()
        [void]$EulaRtfSb.Append("{\rtf1\ansi\deff0{\fonttbl{\f0\fnil\fcharset134 SimSun;}}\uc1`n")
        $InKeySection = $false
        foreach ($EulaLine in $EulaLines) {
            # 加粗范围：重要提示段 + 第三条（使用限制）+ 第五条（免责
            # 声明）全文（含各自小节），其余章节标题关闭加粗状态
            if ($EulaLine -match '^三、使用限制' -or $EulaLine -match '^五、免责声明') {
                $InKeySection = $true
            } elseif ($EulaLine -match '^[一二四六七八九]、') {
                $InKeySection = $false
            }
            if ($InKeySection -or $EulaLine.StartsWith('重要提示：')) {
                [void]$EulaRtfSb.Append('{\b ').Append((ConvertTo-RtfEscape $EulaLine)).Append("\par}`n")
            } else {
                # 注意此处 \par 后没有 }：非加粗行无开组，多余的 } 会提前
                # 闭合整个 RTF 文档组，后续内容全部不被渲染
                [void]$EulaRtfSb.Append((ConvertTo-RtfEscape $EulaLine)).Append("\par`n")
            }
        }
        [void]$EulaRtfSb.Append('}')
        [System.IO.File]::WriteAllText($EulaRtfPath, $EulaRtfSb.ToString(), [System.Text.Encoding]::ASCII)
        Write-Host "== EULA RTF 已生成（加粗重要提示 + 第三/五条全文）：$EulaRtfPath ==" -ForegroundColor Green

        # ---- 6.5 Inno 首装包：向导（磁盘空间检测 / 分区选择 / 每用户
        #      安装）包装 Velopack Setup.exe，经 --installto "{app}"
        #      --silent 静默落盘到用户所选目录 ----
        Write-Host "== Inno Setup 编译首装包（lzma2 压缩，约数分钟）==" -ForegroundColor Cyan
        & $IsccExe `
            "/DAppVersion=$Version" `
            "/DVeloSetupPath=$(Join-Path $UpdateFeedDir "$PackId-win-Setup.exe")" `
            "/DEulaRtfPath=$EulaRtfPath" `
            (Join-Path $BuildDir 'installer_velopack.iss')
        if ($LASTEXITCODE -ne 0) {
            throw "ISCC 编译失败（exit $LASTEXITCODE）"
        }

        # ---- 6.6 Inno 首装包以统一命名进入 feed：删除 vpk 原生
        #      Setup.exe（yangjinduo.E-SubDub-win-Setup.exe），把 Inno 包
        #      以 ESubDub-Setup-<版本>.exe 复制进 feed，发布资产名与本地
        #      产物统一。改名不影响更新链路：releases.win.json 只引用
        #      nupkg，vpk download（下一版生成增量）只拉 Full nupkg，
        #      均不引用 Setup.exe；assets.win.json 的 Installer 条目同步
        #      改名保持元数据一致 ----
        $InnoSetupExe = Join-Path $DistDir "ESubDub-Setup-$Version.exe"
        if (-not (Test-Path -LiteralPath $InnoSetupExe)) {
            throw "Inno 首装包未找到：$InnoSetupExe"
        }
        $VeloSetupName = "$PackId-win-Setup.exe"
        $VeloSetupPath = Join-Path $UpdateFeedDir $VeloSetupName
        if (Test-Path -LiteralPath $VeloSetupPath) {
            Remove-Item -LiteralPath $VeloSetupPath -Force
        }
        Copy-Item -LiteralPath $InnoSetupExe -Destination $UpdateFeedDir
        $AssetsJsonPath = Join-Path $UpdateFeedDir 'assets.win.json'
        if (Test-Path -LiteralPath $AssetsJsonPath) {
            $AssetsJson = Get-Content -Raw -LiteralPath $AssetsJsonPath
            $AssetsJson = $AssetsJson.Replace($VeloSetupName, "ESubDub-Setup-$Version.exe")
            [System.IO.File]::WriteAllText($AssetsJsonPath, $AssetsJson)
        }
        Write-Host "== 首装包已进入 feed（统一命名 ESubDub-Setup-$Version.exe）==" -ForegroundColor Green
    }
}

Write-Host "== 构建完成（E-SubDub v$Version）==" -ForegroundColor Green
