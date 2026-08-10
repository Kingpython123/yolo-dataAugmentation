"""离屏渲染界面并截图, 用于在没有显示器的环境里检查排版与配色。

用 Qt 的 offscreen 平台插件构造真实窗口, 然后 grab() 成 PNG。这比"能 import 就算
过"有意义得多 —— 排版错乱、控件重叠、深色主题下对话框发白这类问题只有看到才知道。

用法:
  python tools/check_gui.py                 # 深色主题
  python tools/check_gui.py --theme light
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONUTF8", "1")
# offscreen 插件自己不带字体, 不指定的话每个字都渲染成方框。
# 指向系统字体目录, 才能看出真实的文字排版。
if sys.platform.startswith("win"):
    _fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if _fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))

OUT_DIR = REPO / "tmp_gui"

# 每页切换后留给后台任务(扫描/体检)的处理时间, 毫秒
SETTLE_MS = 1200

CLEAN_W, CLEAN_H = 600, 1200
BORDER = 24


def build_workspace(root: Path) -> None:
    """造一个数据齐全的工作区, 好让界面上有真实内容可看。"""
    import numpy as np
    import yaml
    from PIL import Image

    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    cfg["data"]["clean_root"] = "clean"
    cfg["data"]["defect_root"] = "defect"
    cfg["api"]["base_url"] = "https://api.example.invalid/v1"
    (root / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    def make_image(path: Path) -> None:
        arr = np.full((CLEAN_H, CLEAN_W, 3), 180, np.uint8)
        arr[:BORDER, :] = 15
        arr[-BORDER:, :] = 15
        arr[:, :BORDER] = 15
        arr[:, -BORDER:] = 15
        Image.fromarray(arr).save(path)

    for cls, count in (("1.jpg", 114), ("1.bmp", 96), ("5.1", 107)):
        d = root / "clean" / cls
        d.mkdir(parents=True, exist_ok=True)
        # 只真造 3 张, 其余用同一张复制, 目的是让界面上的数字看起来真实
        make_image(d / "c0.png")
        for i in range(1, count):
            (d / f"c{i}.png").write_bytes((d / "c0.png").read_bytes())

    crops = root / "outputs" / "catalog" / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(24):
        name = f"ref{i}.png"
        make_image(crops / name)
        records.append({
            "entry_id": f"1.jpg__ref{i}__0",
            "class_name": ["1.jpg", "1.bmp", "5.1"][i % 3],
            "defect_type": "变形" if i % 7 else "划痕",
            "location": "瓶身中部", "appearance": "标签表面多条斜向褶皱",
            "severity": 3 + (i % 3), "count": 4,
            "orientation": "斜向左上-右下", "geometry": "长条状褶皱",
            "extent": "覆盖标签中部", "photometry": "明暗交替",
            "edge_profile": "较锐利", "texture_effect": "文字轻微弯曲",
            "prompt_hint": "diagonal creases",
            "source_image": "../defect/1.jpg/x.jpg",
            "crop_path": f"outputs/catalog/crops/{name}",
            "bbox": [10, 10, 300, 900], "source_size": [CLEAN_W, CLEAN_H],
        })
    (root / "outputs" / "catalog" / "catalog.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")

    # 造一些产物与标注, 让导出页与日志页有数字
    for cls in ("1.jpg", "1.bmp"):
        gen = root / "outputs" / "generated" / cls
        rej = root / "outputs" / "rejected" / cls
        msk = root / "outputs" / "masks" / cls
        for d in (gen, rej, msk):
            d.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            make_image(gen / f"g{i}.png")
            make_image(msk / f"g{i}_mask.png")
        for i in range(2):
            make_image(rej / f"r{i}.png")

    ann = root / "outputs" / "annotations.jsonl"
    lines = []
    for cls in ("1.jpg", "1.bmp"):
        for i in range(6):
            lines.append(json.dumps({
                "class": cls, "generated_image": f"outputs/generated/{cls}/g{i}.png",
                "qc": {"realism": 7.4}, "attempt": 1}, ensure_ascii=False))
        for i in range(2):
            lines.append(json.dumps({
                "class": cls, "accepted_as": "rejected",
                "generated_image": f"outputs/rejected/{cls}/r{i}.png",
                "qc": {"realism": 5.1}, "attempt": 3}, ensure_ascii=False))
    ann.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fail_log = root / "outputs" / "fail_log.jsonl"
    fails = ([json.dumps({"reason": "channel_unavailable", "class": "1.jpg",
                          "stem": f"s{i}"}) for i in range(9)]
             + [json.dumps({"reason": "timeout", "class": "1.jpg",
                            "stem": f"t{i}"}) for i in range(3)]
             + [json.dumps({"reason": "rate_limited", "class": "1.bmp",
                            "stem": "r0"})])
    fail_log.write_text("\n".join(fails) + "\n", encoding="utf-8")


def settle(app, ms: int = SETTLE_MS) -> None:
    """跑一段事件循环, 让后台任务的结果回到界面。

    先等固定时长, 再等到在途任务清零 —— 只等固定时长的话, 扫描慢一点就会截到
    半空的界面, 那种截图看不出是界面有问题还是等得不够。
    """
    from PySide6.QtCore import QEventLoop, QTimer

    from app.gui import tasks as gui_tasks

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

    waited = 0
    while gui_tasks.active_count() and waited < 20000:
        inner = QEventLoop()
        QTimer.singleShot(200, inner.quit)
        inner.exec()
        waited += 200
    app.processEvents()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="dark", choices=("dark", "light"))
    ap.add_argument("--keep", action="store_true",
                    help="保留临时工作区(排查时用)")
    args = ap.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="dsgui_"))
    build_workspace(workspace)

    from app.runtime import paths, settings
    os.environ[paths.WORKSPACE_ENV] = str(workspace)
    # 让界面显示"已配置 key"的状态, 但不写真实凭据
    os.environ["RELAY_API_KEY"] = "sk-demo-key-for-screenshot-only"

    from app.runtime import bootstrap
    bootstrap.install(workspace=workspace)
    settings.update(theme=args.theme, last_classes=["1.jpg"], last_count=700)

    from PySide6.QtWidgets import QApplication

    from app.gui.main_window import MainWindow, PAGE_ORDER

    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1240, 860)
    window.show()
    settle(app, 400)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob(f"{args.theme}-*.png"):
        old.unlink()

    problems: list[str] = []
    for index, (key, _cls) in enumerate(PAGE_ORDER, start=1):
        window.navigate(key)
        settle(app)
        pixmap = window.grab()
        target = OUT_DIR / f"{args.theme}-{index}-{key}.png"
        if not pixmap.save(str(target)):
            problems.append(f"截图保存失败: {target}")
            continue
        print(f"  saved {target.name}  {pixmap.width()}x{pixmap.height()}")

    # 顺手检查每个交互控件都有可访问名称(NFR-6)
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QLineEdit, QPushButton,
                                   QSpinBox, QTableWidget)
    interactive = (QPushButton, QLineEdit, QSpinBox, QComboBox, QCheckBox,
                   QTableWidget)
    missing = []
    for widget_type in interactive:
        # findChildren 只接受单个类型, 不接受元组
        for widget in window.findChildren(widget_type):
            # Qt 自己的内部子控件(如 QSpinBox 内嵌的行编辑器)不由我们命名
            if widget.objectName().startswith("qt_"):
                continue
            label = widget.accessibleName()
            if not label and isinstance(widget, QPushButton):
                label = widget.text()
            if not label:
                missing.append(f"{type(widget).__name__}:{widget.objectName()}")
    if missing:
        problems.append(f"缺少 accessibleName 的控件 {len(missing)} 个: "
                        f"{missing[:8]}")

    if not args.keep:
        import shutil
        shutil.rmtree(workspace, ignore_errors=True)

    print()
    if problems:
        for p in problems:
            print(f"[FAIL] {p}")
        return 1
    print(f"[PASS] 已生成 {len(PAGE_ORDER)} 张截图到 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
