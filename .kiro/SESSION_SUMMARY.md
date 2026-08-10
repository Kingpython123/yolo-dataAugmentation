# 会话总结：桌面程序封装 + 定向生成功能

记录时间：2026-08-10。用于在新会话里快速恢复上下文，不是给最终用户看的说明文档。

对应的规格文档：
- `.kiro/specs/desktop-app-packaging/`（requirements.md / design.md / tasks.md / decisions.md）
- `.kiro/specs/targeted-regeneration/requirements.md`

Git 状态：已提交并推送到 `origin/main`，commit `d18af6b`
`feat: 桌面程序封装 + 驳回样本重生成/定向补充/形态检索`（86 个文件，+12918/-98）。

---

## 一、这次会话做了两件独立的事

### 1. 桌面程序封装

把原有的 CLI 流水线（`run.py` 的 11 个子命令）包装成 PySide6 图形界面程序，**生成算法内核零改动**。

- `app/runtime/` — 路径体系（冻结感知）、用户配置、密钥（Windows 凭据管理器）、日志、`bootstrap.install()` 统一装配入口
- `app/jobs/` — 作业模型，生成任务跑在**分离进程**里，关闭界面不中断任务，重开自动附着；事件流写文件（`events.jsonl`）不走 stdout 管道（避免界面关闭后管道阻塞死锁）
- `app/services/` — 缺陷库体检/安装、数据集扫描（带诊断提示）、导出打包、连通性测试
- `app/gui/` — 6 个页面：概览/缺陷库/设置/生成/导出/日志，深浅两套主题，QRunnable 注意 `setAutoDelete(False)` 否则排队信号会丢失
- `packaging/` — PyInstaller 双 exe（GUI + CLI 共享一份 COLLECT）、Inno Setup 安装包脚本、`opencv-python-headless` 锁 `<5`（默认会装到 OpenCV 5，是大版本跳跃会改变数值行为）
- 冻结后实测：`selftest` 输出与源码版逐字一致（0.281 / 0.00000），安装后占用 234MB

**未完成**：安装包没实际生成（本机没装 Inno Setup 6）；缺陷库数据包只跑过 `--dry-run` 没真正打出来；没在无 Python 的干净机器走完整流程；单元测试按用户要求推迟。

### 2. 三个新生成命令（用户在封装完成后追加的需求）

用户提了两个需求：①人工 review 驳回的照片能否用原参考重新生成；②训练后发现某类褶皱识别差，需要补充类似形态的样本。全部通过 CLI 提供，**核心生成算法唯一改动是 `Task.salt` 参与 RNG 播种**（为空时播种字符串逐字不变，不影响任何既有任务的可复现性）。

| 命令 | 解决什么 | 关键实现 |
|---|---|---|
| `regen-rejected` | 需求① | 从产出文件名反查(类别,干净图,参考条目)，不依赖 annotations.jsonl。产物落 `outputs/regenerated/`，不自动合并进训练集 |
| `gen-augment` | 需求② | 三种挑选方式可组合取并集：方式A按属性筛(`--severity`/`--orientation`等)、方式C从产出图反查(`--from-images`)、方式B形态相似检索(`--similar-to-image`/`--similar-to-entry`)。默认强制预览，加 `--yes` 才真跑 |
| `find-similar` | 需求②独立入口 | 离线形态检索，不调 API |

新增核心模块：
- `src/rejection.py` — 文件名反查。原理：正向对缺陷库每条 `entry_id` 算 `safe_name()` 建索引表，用文件名里的 ref 片段查表（不做字符串逆运算，`safe_name` 的括号/斜杠替换不可逆）
- `src/catalog_query.py` — 按属性筛选（`QueryFilter`），orientation/geometry 是自由文本只能关键词包含匹配
- `src/morphology.py` — 形态相似检索。基于 `structure_ref.defect_structure_map` 的浮雕图算方向直方图+多尺度带通能量+连通域统计+方向集中度，缓存到 `outputs/catalog/morphology.npz`
- `src/reporting.py` / `src/catalog_report.py` — 桌面封装阶段就有的输出边界与体检逻辑提炼，`gen-augment` 等复用

**验证方式**：全部用真实数据跑通（668条缺陷库、30张真实驳回样本"没打标签的"目录），关键数字：反查30/30全中；属性筛选 severity 4-5 命中473/668；方式C+方式A并集精确吻合独立统计值；形态检索自检25/25排第一，走向命中率0.863 vs 基线0.724（+19.1%）。验证脚本在 `tools/check_*.py`，可随时重跑。

---

## 二、需要新会话知道的关键约束

### 本机工具编码陷阱（极重要，反复踩过）

- **`str_replace` 工具会把 UTF-8 中文文件按 GBK 往返写坏**，导致整文件乱码。这次会话里发生过至少 3 次（`generate.py`、`.gitignore` 两次）。**规则：对含中文内容的文件，一律不用 `str_replace`，改用下面两种方式之一**：
  1. 写一个纯 ASCII 的 Python 一次性脚本（中文用 `\uXXXX` 转义），做精确字符串替换后写回（`encoding="utf-8", newline=""` 保留原换行符）
  2. 全文件重写用 `fs_write`，但 `fs_write` 本身编码也不稳定（有时 UTF-8 有时 GBK），写完必须过一遍 `tools/normalize_text.ps1` 校验/转码
- **PowerShell 5.1 的 `>` 重定向默认写 UTF-16LE 带 BOM**，导致 `Get-Content`/后续读取出现乱码或解码失败。验证脚本要么用 Python 内部 `contextlib.redirect_stdout` 捕获（见 `tools/regression_check.py`），要么用 `subprocess.run(..., encoding='utf-8')` 从 Python 侧拿 git 输出（比直接读 PowerShell 终端显示可靠得多，PowerShell 控制台本身是 GBK 显示）
- **带中文的 `.ps1` 文件必须有 UTF-8 BOM**，否则 Windows PowerShell 5.1 按系统 ANSI(GBK) 解码，中文注释会把字符串字面量弄坏导致语法错误。`tools/add_ps1_bom.ps1` 处理这个
- 判断文件内容是否正常，**别信终端里打印出来的中文**（十有八九显示乱码但文件是对的，或反过来），要用 `read_file` 工具直接读，或者 Python 脚本里 `assert` 断言关键字符串存在

### 生成内核的不变约束

- `src/generate.py` 的 `_run_tasks` / `Task` 等改动必须保证：不传新增可选参数时行为与改造前**逐字一致**。回归靠 `tools/regression_check.py` 比对 `baseline/selftest.txt` 和 `baseline/inspect.txt`（这两个基线文件本身是 UTF-16LE 编码的历史遗留，读取要按 BOM 探测）
- `outputs/catalog/`（缺陷库）不要迁移路径，`config.yaml` 的 `output.catalog` 相对路径解析依赖它待在原处，且 `annotations.jsonl` 里记录的路径字符串是断点续跑判重的依据
- 缺陷库里约 540/668 条 `source_image` 字段是建库机器的绝对路径（可移植性问题，`packaging/make_catalog_pack.py` 打包时会归一化，仓库内原始文件未改）

---

## 三、待办事项（按优先级）

1. **补齐安装包**：装 Inno Setup 6 后跑 `packaging/build.ps1`，会自动打包+冒烟验证+生成 Setup.exe
2. **打出缺陷库数据包**：`python packaging/make_catalog_pack.py -o dist/catalog-data.zip`（之前只跑过 `--dry-run`）
3. **阶段三形态检索可以考虑接入 GUI**（当前只有 CLI，`find-similar`/`gen-augment` 用户明确说"先不接界面"）
4. **单元测试**：按用户要求推迟，当前用 `tools/check_*.py` 系列脚本兜底，不是正式测试套件
5. 用户可能会在无 Python 的干净机器上验证桌面程序完整流程（安装→引导→装缺陷库→生成→导出），这一步这次会话没做完

## 四、如果新会话要继续开发

- 先读 `.kiro/specs/desktop-app-packaging/decisions.md`，里面记录了12条实施期相对设计文档的偏离和真实缺陷（比如 QRunnable 生命周期、QSS 背景穿透等），避免重复踩坑
- 生成新代码前先跑一次 `tools/regression_check.py` 确认基线仍然一致，作为改动前后对比的起点
- 涉及中文文件的任何编辑，先看本文档"编码陷阱"一节
