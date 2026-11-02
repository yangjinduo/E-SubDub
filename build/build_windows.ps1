# E-SubDub 一键构建脚本（Windows）
# ====================================================================
# 产物：
#   build\dist\ESubDub\                        onedir 发行目录
#   build\dist\ESubDub-Setup-<版本>.exe       Inno Setup 安装包
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
#   2. （可选）Inno Setup 6（ISCC 在 PATH）——缺失时自动跳过安装包
#      步骤，只产 onedir 目录：
#        winget install -e --id JRSoftware.InnoSetup
#   3. C 编译器：优先 MSVC（VS Build Tools 的 VCTools）；缺失时
#      Nuitka 经 --assume-yes-for-downloads 自动下载 MinGW64 也可用。
#   4. 便携 CPython（python-build-standalone，随安装包分发给最终
#      用户）：提前手动下载解压到 build\runtime\python\；缺失时
#      脚本打印下载链接并中止（不自动下载）
#
# 用法（在项目根、已激活构建 venv 的终端中）：
#   .\build\build_windows.ps1                # 完整构建（含安装包）
#   .\build\build_windows.ps1 -SkipInstaller # 只产 onedir 发行目录
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

# 便携 CPython → 发行目录 runtime\python（env_setup 创建 venv 依赖它）；
# Copy-Item 不创建中间目录，先显式建 runtime
New-Item -ItemType Directory -Force -Path (Join-Path $AppDist 'runtime') | Out-Null
Copy-Item -Recurse -Force `
    -Path (Join-Path $RuntimeStaging 'python') `
    -Destination (Join-Path $AppDist 'runtime\python')

Write-Host "== onedir 发行目录就绪：$AppDist ==" -ForegroundColor Green

# ---------- 6. Inno Setup 安装包 ----------
if ($SkipInstaller) {
    Write-Host "== 已按 -SkipInstaller 跳过安装包步骤 ==" -ForegroundColor Yellow
} else {
    $Iscc = Get-Command ISCC -ErrorAction SilentlyContinue
    if (-not $Iscc) {
        Write-Warning "未找到 ISCC（Inno Setup 6），已跳过安装包步骤。安装后重跑：winget install -e --id JRSoftware.InnoSetup"
    } else {
        Write-Host "== Inno Setup 编译安装包（lzma2 压缩，约数分钟）==" -ForegroundColor Cyan
        & $Iscc.Source "/DAppVersion=$Version" "/DDistDir=$AppDist" (Join-Path $BuildDir 'installer.iss')
        if ($LASTEXITCODE -ne 0) {
            throw "ISCC 编译失败（exit $LASTEXITCODE）"
        }
        Write-Host "== 安装包就绪：$(Join-Path $DistDir "ESubDub-Setup-$Version.exe") ==" -ForegroundColor Green
    }
}

Write-Host "== 构建完成（E-SubDub v$Version）==" -ForegroundColor Green
