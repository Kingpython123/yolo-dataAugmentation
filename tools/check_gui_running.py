"""截取"生成任务运行中"的真实界面状态。

做法: 复用 check_jobs.py 的本地 HTTP 桩(冒充中转站, 不联网不花钱), 造一个真实
可跑的工作区, 通过 job_service.submit() 启动一个真正的分离 worker 进程, 然后
打开 GUI 的生成页 —— 这时候 GeneratePage 会自动附着到这个正在跑的作业, 进度条、
统计卡片、日志都是从真实事件流里读出来的, 不是摆拍的假状态。

用法:
  python tools/check_gui_running.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONUTF8", "1")
if sys.platform.startswith("win"):
    _fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if _fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))

OUT_DIR = REPO / "tmp_gui"

# 桩服务里编辑一次的耗时(见 check_jobs.EDIT_DELAY_SECONDS), 决定了任务推进速度。
# 这里在跑到"已完成几张但还没完事"的中间点截图, 而不是刚启动或已经跑完。
SHOTS_AT_DONE = (1, 3)


def settle(app, ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def main() -> int:
    import check_jobs as jobs_harness  # 复用桩服务与工作区搭建

    server, base_url = jobs_harness.start_stub()
    print(f"桩服务: {base_url}")

    import shutil
    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix="dsgui_running_"))
    # count 给足够多, 保证截图窗口内任务还在跑, 不会提前跑完
    jobs_harness.build_workspace(workspace, base_url, n_clean=8)

    os.environ["RELAY_API_KEY"] = "sk-stub-key-local-only"
    os.environ["RELAY_BASE_URL"] = base_url

    from app.runtime import bootstrap, paths, settings
    os.environ[paths.WORKSPACE_ENV] = str(workspace)
    bootstrap.install(workspace=workspace)
    settings.update(theme="dark")

    from app.services import job_service

    print("提交真实作业(真实分离进程) ...")
    status = job_service.submit(["T1"], 8)
    print(f"  job_id={status.job_id}  pid={status.pid}")

    from PySide6.QtWidgets import QApplication

    from app.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1240, 860)
    window.show()
    settle(app, 300)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("running-*.png"):
        old.unlink()

    from app.jobs import store

    saved = 0
    try:
        window.navigate("generate")  # 触发自动附着到正在跑的作业
        settle(app, 500)

        deadline = time.time() + 60
        last_done = -1
        while time.time() < deadline and saved < len(SHOTS_AT_DONE):
            settle(app, 700)  # 让 GeneratePage 的轮询定时器有机会跑一次
            page = window._pages["generate"]
            page._poll()  # 界面定时器是 1 秒一次, 手动催一下以缩短截图等待

            progress = page._progress
            if progress.done != last_done:
                print(f"  已完成 {progress.done}/{progress.pending}, "
                     f"合格 {progress.ok}, 日志 {len(progress.logs)} 行")
                last_done = progress.done

            if progress.done >= SHOTS_AT_DONE[saved] or (
                    saved == 0 and progress.done >= 1):
                target = OUT_DIR / f"running-{saved + 1}-generate.png"
                pixmap = window.grab()
                pixmap.save(str(target))
                print(f"  saved {target.name} (完成 {progress.done} 张时)")
                saved += 1

        if saved == 0:
            print("  未能在超时前捕捉到任何进度, 直接截当前状态兜底")
            pixmap = window.grab()
            target = OUT_DIR / "running-1-generate.png"
            pixmap.save(str(target))
            saved = 1

        # 再截一张日志页, 看真实产生的运行日志
        window.navigate("logs")
        settle(app, 500)
        window.grab().save(str(OUT_DIR / "running-3-logs.png"))
        print("  saved running-3-logs.png")

        # 主动请求停止, 不让作业一直跑到底占资源
        if status.job_dir is not None:
            job_service.cancel(status)
            settle(app, 2000)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(workspace, ignore_errors=True)

    print(f"\n完成, 共 {saved} 张运行中截图 + 1 张日志页截图, 位于 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
