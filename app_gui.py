"""图形界面入口。

打包后对应 DefectSynth.exe(windowed)。CLI 与 worker 走 run.py / defectsynth-cli.exe。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.runtime import bootstrap, paths  # noqa: E402
from app.version import APP_DISPLAY_NAME, APP_NAME, __version__  # noqa: E402


def main() -> int:
    # 装配要在创建 QApplication 之前完成: 它会固定标准流编码、把默认配置指向
    # 工作区、并从系统凭据管理器取出 API key 注入环境。
    bootstrap.install(log_name="gui.log")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # 高分屏下的图标与位图缩放。Qt6 已默认开启 DPI 缩放, 这里只补图标策略。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    # 让 Windows 任务栏把 GUI 与 CLI 视作同一个应用
    app.setOrganizationName(APP_NAME)

    from app.gui.main_window import MainWindow

    window = MainWindow()
    if not window.ensure_workspace():
        return 0
    paths.ensure_workspace_layout()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
