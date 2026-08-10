# 一键构建: 清理 -> 打包 -> 冒烟验证 -> 生成安装包 -> 体积报告
#
# 冒烟验证是刻意放进构建脚本而不是靠人工的: "Qt 平台插件缺失" 与 "cv2 隐式导入
# 缺失" 这两类问题只在冻结后暴露, PyInstaller 打包成功完全不代表程序能启动。
#
# 用法:
#   & .\packaging\build.ps1
#   & .\packaging\build.ps1 -SkipInstaller     # 只出 dist 目录, 不做安装包
#   & .\packaging\build.ps1 -Python .\.venv\Scripts\python.exe

param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$SkipInstaller,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $RepoRoot

# 安装包体积上限, 对应 requirements.md FR-1.3
$MaxInstallerMB = 120
$MaxInstalledMB = 300

function Write-Step($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

function Fail($text) {
    Write-Host "构建失败: $text" -ForegroundColor Red
    Pop-Location
    exit 1
}

try {
    if (-not (Test-Path $Python)) {
        Fail "找不到 Python: $Python (先建虚拟环境并安装 requirements/build.txt)"
    }

    # ---- 版本号: 以 app/version.py 为唯一来源 ----
    $Version = & $Python -c "import sys; sys.path.insert(0,'.'); from app.version import __version__; print(__version__)"
    if (-not $Version) { Fail "无法读取 app/version.py 里的版本号" }
    $Version = $Version.Trim()
    Write-Host "版本: $Version"

    # ---- 依赖自检 ----
    Write-Step "检查依赖"
    & $Python -c @"
import importlib, sys
missing = []
for mod in ('PySide6', 'cv2', 'numpy', 'PIL', 'yaml', 'requests', 'tqdm',
            'keyring', 'PyInstaller'):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
if missing:
    print('缺少依赖: ' + ', '.join(missing))
    sys.exit(1)
import cv2
if cv2.__version__.split('.')[0] != '4':
    print('opencv 主版本应为 4, 实际 ' + cv2.__version__)
    sys.exit(1)
try:
    import PySide6.QtWebEngineCore  # noqa: F401
    print('警告: 环境里存在 QtWebEngine, 打包会把它排除, 但请确认未被业务代码使用')
except ImportError:
    pass
print('依赖检查通过, opencv ' + cv2.__version__)
"@
    if ($LASTEXITCODE -ne 0) { Fail "依赖检查未通过" }

    # ---- 清理 ----
    Write-Step "清理旧产物"
    foreach ($dir in @("build", "dist")) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            Write-Host "已删除 $dir"
        }
    }

    # ---- 打包 ----
    Write-Step "PyInstaller 打包"
    & $Python -m PyInstaller --noconfirm --clean packaging\app.spec
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 打包失败" }

    $DistDir = Join-Path $RepoRoot "dist\DefectSynth"
    $GuiExe = Join-Path $DistDir "DefectSynth.exe"
    $CliExe = Join-Path $DistDir "defectsynth-cli.exe"
    foreach ($exe in @($GuiExe, $CliExe)) {
        if (-not (Test-Path $exe)) { Fail "打包后找不到 $exe" }
    }

    # ---- 冒烟验证 ----
    if (-not $SkipSmoke) {
        Write-Step "冒烟验证"

        # 用临时工作区, 避免污染开发目录
        $SmokeWs = Join-Path $env:TEMP ("defectsynth-smoke-" + [guid]::NewGuid().ToString("N").Substring(0,8))
        New-Item -ItemType Directory -Path $SmokeWs | Out-Null
        $env:DEFECTSYNTH_WORKSPACE = $SmokeWs
        try {
            # 1) selftest 走完整的图像链路, 能验证 cv2/numpy/Pillow 的隐式导入是否齐全
            Write-Host "  [1/2] defectsynth-cli.exe selftest"
            $out = & $CliExe selftest 2>&1 | Out-String
            if ($LASTEXITCODE -ne 0) {
                Write-Host $out
                Fail "selftest 未通过(通常是 cv2/numpy 隐式导入缺失)"
            }
            if ($out -notmatch "0\.281") {
                Write-Host $out
                Fail "selftest 的分割前景占比与基线 0.281 不一致, 行为可能已改变"
            }
            if ($out -notmatch "0\.00000") {
                Write-Host $out
                Fail "selftest 的掩膜外像素变化不为 0, 硬约束被破坏"
            }
            Write-Host "        分割与回贴数值与基线一致" -ForegroundColor Green

            # 2) GUI 起一次再退出, 验证 Qt 平台插件目录完整
            #    (缺插件的典型症状是 could not load the Qt platform plugin "windows")
            Write-Host "  [2/2] DefectSynth.exe 离屏启动"
            $env:QT_QPA_PLATFORM = "offscreen"
            $proc = Start-Process -FilePath $GuiExe -PassThru -WindowStyle Hidden
            Start-Sleep -Seconds 8
            if ($proc.HasExited -and $proc.ExitCode -ne 0) {
                Fail "GUI 启动即退出, 退出码 $($proc.ExitCode)(通常是 Qt 平台插件缺失)"
            }
            if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit() }
            Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
            Write-Host "        Qt 平台插件可加载" -ForegroundColor Green
        }
        finally {
            Remove-Item Env:\DEFECTSYNTH_WORKSPACE -ErrorAction SilentlyContinue
            Remove-Item -Recurse -Force $SmokeWs -ErrorAction SilentlyContinue
        }
    }

    # ---- 体积报告 ----
    Write-Step "体积"
    $InstalledMB = [math]::Round(((Get-ChildItem $DistDir -Recurse -File |
        Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "安装后占用: $InstalledMB MB (上限 $MaxInstalledMB MB)"
    if ($InstalledMB -gt $MaxInstalledMB) {
        Write-Host "  超出上限, 检查 app.spec 的 excludes 是否生效" -ForegroundColor Yellow
    }

    # ---- 安装包 ----
    if ($SkipInstaller) {
        Write-Host ""
        Write-Host "已跳过安装包生成。产物: $DistDir" -ForegroundColor Green
        Pop-Location
        exit 0
    }

    Write-Step "生成安装包"
    $iscc = $null
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $candidate) { $iscc = $candidate; break }
    }
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        Write-Host "未找到 Inno Setup 6 的 ISCC.exe, 跳过安装包生成。" -ForegroundColor Yellow
        Write-Host "安装 Inno Setup 后重跑, 或用 -SkipInstaller 明确跳过。" -ForegroundColor Yellow
        Write-Host "产物: $DistDir" -ForegroundColor Green
        Pop-Location
        exit 0
    }

    & $iscc "/DMyAppVersion=$Version" (Join-Path $PSScriptRoot "installer.iss")
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup 生成安装包失败" }

    $installer = Get-ChildItem (Join-Path $RepoRoot "dist") -Filter "*Setup*.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($installer) {
        $mb = [math]::Round($installer.Length / 1MB, 1)
        Write-Host ""
        Write-Host "安装包: $($installer.FullName) ($mb MB, 上限 $MaxInstallerMB MB)" -ForegroundColor Green
        if ($mb -gt $MaxInstallerMB) {
            Write-Host "  超出上限, 检查 excludes" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "构建完成。别忘了单独制作缺陷库数据包:" -ForegroundColor Green
    Write-Host "  $Python packaging\make_catalog_pack.py -o dist\catalog-data.zip"
}
finally {
    Pop-Location
}
