# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置: 一份 COLLECT 产出两个 exe。

为什么要两个 exe
--------------
GUI 必须 console=False, 否则每次启动都弹一个黑框; CLI 必须 console=True, 否则
没有控制台可以输出。PyInstaller 支持在同一个 spec 里定义多个 EXE 并共享一份
COLLECT, 这样两者共用同一套 DLL 与 Python 运行时, 不会把体积翻倍。

  DefectSynth.exe       windowed  图形界面
  defectsynth-cli.exe   console   命令行 + 后台执行进程(worker 子命令)

界面启动后台任务时调用 defectsynth-cli.exe worker --job <dir>(见 app/jobs/launcher.py)。

为什么用 onedir 而不是 onefile
----------------------------
onefile 每次启动都要把约 200 MB 解压到临时目录, 启动慢, 且长跑期间磁盘上存在
双份数据。onedir 配合安装包分发是更合适的形态。
"""
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821 - PyInstaller 注入
REPO = SPEC_DIR.parent

# 随程序分发的只读资源。缺陷库(约 440 MB)刻意不在此列, 它作为独立数据包分发。
DATAS = [
    (str(REPO / "config.yaml"), "."),
]

HIDDEN_IMPORTS = [
    # keyring 的后端是运行时动态发现的, 不显式声明会在冻结后找不到,
    # 表现为"能启动但保存 API key 时报没有可用后端"
    "keyring.backends.Windows",
    "keyring.backends.fail",
]

# Qt 的模块很多而这里只用了 QtWidgets/QtGui/QtCore。不裁剪的话 QtWebEngine 之类
# 会让包体多出上百 MB。
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvgWidgets",
    # 项目里没用到的重型科学栈; 若被间接拉进来会白占几十 MB
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
    # PIL.ImageQt 会引入另一套 Qt 绑定的探测逻辑, 项目并不需要
    "PIL.ImageQt",
]

RUNTIME_HOOKS = [str(SPEC_DIR / "runtime_hook_utf8.py")]

# 实测第一次构建: opencv 自带的 FFmpeg DLL 占 29.1 MB, 是整个发行版里
# 第二大的文件。它只服务 cv2.VideoCapture / VideoWriter, 而本项目一个都没用,
# 因此去掉是纯体积收益。
EXCLUDED_BINARY_PATTERNS = (
    "opencv_videoio_ffmpeg",
)


def _strip_binaries(binaries):
    """按文件名前缀剔除用不到的动态库。"""
    kept = []
    for entry in binaries:
        name = Path(entry[0]).name.lower()
        if any(name.startswith(p) for p in EXCLUDED_BINARY_PATTERNS):
            continue
        kept.append(entry)
    return kept

_common = dict(
    pathex=[str(REPO)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

gui_analysis = Analysis([str(REPO / "app_gui.py")], **_common)
cli_analysis = Analysis([str(REPO / "run.py")], **_common)

# 合并两个入口的依赖树, 避免同一个模块在两个 PYZ 里各存一份
MERGE = None
try:
    from PyInstaller.building.build_main import MERGE  # noqa: F811
except ImportError:  # pragma: no cover - 老版本 PyInstaller
    MERGE = None
if MERGE is not None:
    MERGE((gui_analysis, "app_gui", "DefectSynth"),
          (cli_analysis, "run", "defectsynth-cli"))

gui_analysis.binaries = _strip_binaries(gui_analysis.binaries)
cli_analysis.binaries = _strip_binaries(cli_analysis.binaries)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="DefectSynth",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 图形界面: 不要控制台黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="defectsynth-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 命令行与 worker: 需要控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DefectSynth",
)
