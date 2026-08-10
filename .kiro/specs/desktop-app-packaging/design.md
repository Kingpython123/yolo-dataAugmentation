# 设计文档：缺陷合成流水线桌面化封装

对应需求：`requirements.md`。本文档只描述结构与契约，不含实现代码。

## 1. 设计原则

- P1 **现有流水线是资产，不是改造对象。** `src/` 下的生成、掩膜、质检逻辑按“只加边界、不动内核”处理。所有新增能力放在新的 `app/` 层。
- P2 **依赖方向单向：`app` → `src`，绝不反向。** 核心层不允许 import 任何界面或 Qt 相关模块（对应 NFR-4）。
- P3 **CLI 与 GUI 共用同一套核心 API。** GUI 不是“调用命令行的壳”，而是与 CLI 平级的另一个适配器。
- P4 **默认行为向后兼容。** 未配置工作区、未安装凭据时，源码方式运行的行为与改造前逐字一致。

## 2. 分层结构

```
┌─────────────────────────────────────────────────────┐
│  适配器层   run.py (CLI)      app/gui (PySide6)      │
└──────────────────┬──────────────────┬───────────────┘
                   │                  │
┌──────────────────▼──────────────────▼───────────────┐
│  应用层  app/jobs (作业编排)  app/services (业务服务) │
│          app/runtime (路径/配置/密钥/日志/输出边界)   │
└──────────────────────────┬──────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────┐
│  核心层  src/ generate · mask_utils · quality_check  │
│          defect_catalog · api_client · config        │
│          src/reporting.py (输出边界协议 + 控制台实现) │
└─────────────────────────────────────────────────────┘
```

`src/reporting.py` 放在核心层而非 `app/`，因为核心层需要自带默认实现（控制台），否则 `src` 会反向依赖 `app`，违反 P2。

## 3. 目录布局

```
photo_difussion/
├── run.py                      CLI 入口（保留，改为薄壳）
├── app_gui.py                  GUI 入口
├── config.yaml                 默认配置模板（同时作为打包内置资源）
├── src/                        核心层（最小改动）
│   ├── reporting.py            新增：Reporter 协议 + ConsoleReporter
│   ├── catalog_report.py       新增：缺陷库体检的结构化结果
│   ├── config.py               改：默认 config 路径走工作区
│   ├── generate.py             改：print/tqdm 迁移 + 取消支持
│   ├── defect_catalog.py       改：print/tqdm 迁移
│   ├── requalify.py            改：print 迁移
│   └── (mask_utils / quality_check / api_client / dataset / structure_ref 不动)
├── app/
│   ├── version.py              版本号单一来源
│   ├── runtime/
│   │   ├── paths.py            冻结感知的路径体系
│   │   ├── settings.py         用户配置持久化
│   │   ├── secrets.py          Windows 凭据管理器
│   │   ├── reporters.py        JsonlReporter / LoggingReporter / TeeReporter
│   │   ├── logging_setup.py    日志配置 + 轮转 + 密钥脱敏
│   │   └── encoding.py         强制 UTF-8 标准流
│   ├── jobs/
│   │   ├── spec.py             JobSpec / JobStatus
│   │   ├── protocol.py         事件类型常量与编解码
│   │   ├── store.py            作业目录读写、附着、列举
│   │   └── launcher.py         分离进程启动与取消
│   ├── services/
│   │   ├── catalog_service.py  缺陷库体检 + 数据包安装
│   │   ├── dataset_service.py  数据目录扫描
│   │   ├── export_service.py   成果导出
│   │   └── api_service.py      连通性测试
│   ├── worker.py               后台执行进程入口
│   └── gui/
│       ├── main_window.py      主窗口 + 侧边导航
│       ├── theme.py            调色板与 QSS 加载
│       ├── resources/theme.qss
│       ├── widgets/            Card / StatCard / StatusDot / LogView / ClassTable
│       └── pages/              overview / generate / catalog / export / settings / logs
├── packaging/
│   ├── app.spec                PyInstaller 配置（双 exe 单 COLLECT）
│   ├── runtime_hook_utf8.py
│   ├── build.ps1               清理 → 打包 → 冒烟 → 生成安装包
│   ├── installer.iss           Inno Setup 脚本
│   └── make_catalog_pack.py    缺陷库数据包制作
├── requirements/
│   ├── runtime.txt             锁定版本（运行期）
│   └── build.txt               锁定版本（构建期）
└── tools/to_utf8.ps1
```

## 4. 路径体系（DD1）

三个互不混淆的概念：

| 概念 | 冻结环境 | 源码环境 | 可写 | 内容 |
|---|---|---|---|---|
| 安装根目录 `install_root()` | exe 所在目录 | 仓库根 | 否 | exe、Qt 插件、DLL |
| 打包资源 `resource_path(rel)` | `sys._MEIPASS / rel` | 仓库根 `/ rel` | 否 | 默认 `config.yaml`、QSS、图标 |
| 工作区 `workspace()` | 用户指定 | 用户指定，缺省=仓库根 | 是 | `config.yaml` `outputs/` `logs/` `jobs/` |

`workspace()` 解析顺序：

1. 环境变量 `DEFECTSYNTH_WORKSPACE`
2. 用户配置 `%APPDATA%\DefectSynth\settings.json` 的 `workspace`
3. 源码环境回退到仓库根（保证 P4 向后兼容）
4. 冻结环境且以上皆无 → 抛 `WorkspaceNotConfigured`，由 GUI 引导设置

**关键取舍：缺陷库仍位于 `工作区/outputs/catalog/`，不迁到 `工作区/catalog/`。**
理由是 `config.yaml` 的 `output.catalog` 当前值为 `outputs/catalog`，而 `Config.resolve()` 按“相对 config.yaml 所在目录”解析。工作区里放着 `config.yaml`，因此该相对路径天然指向工作区内。不改路径即不改语义，同时保住 C8（`annotations.jsonl` 里记录的是路径字符串，续跑靠它推导 stem）。

## 5. 配置与密钥（DD2、DD7）

### 5.1 配置分层

`load_config(path=None)` 的 `path=None` 分支由 `PROJECT_ROOT / "config.yaml"` 改为工作区内的 `config.yaml`。
工作区内不存在时，从 `resource_path("config.yaml")` 复制一份过去，用户可继续手工编辑高级参数。

`Config.resolve()`、`Config.out_path()` 一行不改。`PROJECT_ROOT` 保留并标注为仅供源码兼容。

优先级（低到高）：内置模板 → 工作区 `config.yaml` → `settings.json` 的覆盖项 → 环境变量 → 界面本次运行的参数。
环境变量分支（`RELAY_API_KEY` / `RELAY_BASE_URL`）保持现有代码不动，满足 FR-3.4。

### 5.2 密钥

- 存储：`keyring` + Windows 凭据管理器，service `DefectSynth`，entry `relay_api_key`。
- 读取顺序：`RELAY_API_KEY` 环境变量 → 凭据管理器 → 报错。
- 界面显示：仅后 4 位，形如 `sk-****...3f9a`。
- 脱敏：`logging` 注册 Filter，把 `sk-` 后接 8 位以上的串替换为 `sk-***`。事件流与崩溃报告走同一 Filter。
- 明确不做：不写入 `config.yaml`、不写入 `settings.json`、不进 exe。

## 6. 输出边界（DD3）

### 6.1 协议

`src/reporting.py` 定义：

```
Reporter (Protocol)
  log(level: str, msg: str)
  event(kind: str, payload: dict)
  progress(done: int, total: int, desc: str)

模块级便捷函数（核心层内部统一调用这一组）
  info(msg) / warn(msg) / event(kind, **fields)
  track(iterable, total, desc) -> Iterator   # 替代 tqdm
  set_reporter(r) / get_reporter()
```

默认 reporter 为 `ConsoleReporter`：`log` 走 `print`、`track` 走 `tqdm`。因此 CLI 输出与改造前逐字一致（这是 FR-2.3 的验收依据）。

### 6.2 实现矩阵

| 实现 | 位置 | 用途 |
|---|---|---|
| `ConsoleReporter` | `src/reporting.py` | CLI 默认，保持原输出 |
| `JsonlReporter` | `app/runtime/reporters.py` | worker 进程，写 `events.jsonl` |
| `LoggingReporter` | `app/runtime/reporters.py` | 转 `logging`，落 `logs/` |
| `TeeReporter` | `app/runtime/reporters.py` | 组合多个 |

### 6.3 迁移范围

机械替换，不改逻辑：

- `src/generate.py`：8 处 `print`、1 处 `tqdm`
- `src/defect_catalog.py`：4 处 `print`、`tqdm` 导入
- `src/requalify.py`：约 6 处 `print`
- `run.py` 的各 `cmd_*`：保留裸 `print`（它们是 CLI 表现层，本就该直接输出）

结构化事件在 `_run_tasks` 内新增，覆盖现有已有统计点，不新增副作用：

| 事件 | 触发点 | 字段 |
|---|---|---|
| `plan` | 任务列表就绪 | `total` `skipped` `pending` |
| `task` | 单任务落盘后 | `stem` `class` `verdict` `realism` `attempt` |
| `progress` | 每完成一项 | `done` `total` |
| `stats` | 周期性 | `ok` `best_effort` `rejected` `failed` `cancelled` |
| `finished` | 收尾 | `status` `stats` |

`verdict` 取值 `ok` / `best_effort` / `rejected` / `failed` / `cancelled`，与现有 `stats` 字典一一对应。

## 7. 作业模型（DD4、DD6）

### 7.1 作业目录

每次运行在 `工作区/jobs/<job_id>/` 下：

```
spec.json      作业参数（类别、数量、force、并发）
status.json    state / pid / started_at / finished_at / stats
events.jsonl   事件流，追加写
worker.log     worker 的 stdout+stderr 兜底
cancel.flag    存在即请求停止
```

`job_id` = `时间戳-短随机`，保证可排序且不冲突。

### 7.2 GUI 与 worker 的解耦

**不用管道，用文件。** worker 以分离进程启动（`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`），stdout/stderr 重定向到 `worker.log`，事件写入 `events.jsonl`。GUI 通过 tail `events.jsonl` 获取进度。

这个选择解决三个问题：

1. FR-5.8 关掉界面不中断任务 —— worker 不是 GUI 的子进程生命周期附属。
2. 管道背压 —— 若走 stdout 管道，GUI 关闭后管道无人读取，worker 写满缓冲区即阻塞死锁。
3. 重新附着 —— GUI 重启后扫描 `jobs/`，发现 `state=running` 且 pid 存活即自动接续显示，无需任何额外机制。

worker 内部把 `sys.stdout` 替换为转发到 stderr 的包装，防止任何遗漏的裸 `print` 污染事件文件；tqdm 在 worker 模式下禁用。

### 7.3 优雅停止（DD5）

- GUI 创建 `cancel.flag`。
- worker 守护线程每秒轮询该文件，命中即置位 `threading.Event`。
- `src/generate.py` 的 `_run_tasks` 新增可选参数 `cancel: threading.Event | None`：
  - `work()` 函数入口检查，已置位则立即返回取消标记，不发起任何 API 调用；
  - 主循环把取消标记计入 `stats["cancelled"]`，不计入 `failed`，不写 `fail_log.jsonl`。
- 已进入在途的任务（最多 `max_workers` 个）跑完并正常落盘。

为什么用文件标志而非信号：Windows 上跨进程 `SIGINT` 不可靠，`terminate` 会直接杀死进程导致 `annotations.jsonl` 写入中断。文件标志同时满足“GUI 关闭后重开仍能取消”。

落盘写入完全沿用现有 `ann_lock` + `flush`，不新增写入路径（对应风险表中的高风险项）。

## 8. 缺陷库数据包（DD8）

### 8.1 制作（`packaging/make_catalog_pack.py`）

1. 读取 `outputs/catalog/catalog.json`。
2. 归一化 `source_image`：绝对路径按 `class_name` + 文件名重建为 `../实拍正样本（有缺陷）/<class>/<name>`，与现有相对路径条目统一（对应 C5、FR-4.4）。
3. 输出 `catalog-data.zip`，内含 `catalog/catalog.json`、`catalog/crops/*`、`catalog/analyzed.json`、`manifest.json`。
4. `manifest.json` 记录条目数、裁剪图数、各文件 SHA-256、制作时间与源 git commit。

### 8.2 安装与体检（`app/services/catalog_service.py`）

- `install_pack(zip)`：校验 manifest → 解压到 `工作区/outputs/catalog/` → 复验。
- `inspect()`：复用从 `run.py::cmd_inspect` 提炼到 `src/catalog_report.py` 的逻辑，返回结构化结果（总数、按类型/严重度/类别分布、裁剪图缺失、缺 `source_size`、超 `max_patch_size` 的条目），CLI 打印它、GUI 渲染它。同一份判定逻辑，两个展现。

## 9. GUI 设计（DD9）

### 9.1 信息架构

侧边导航 6 项，右侧内容区：

| 页面 | 职责 |
|---|---|
| 概览 | 四张状态卡片：工作区、缺陷库、数据集、API 连通性。每张给状态、一句话结论、下一步操作按钮 |
| 生成 | 类别多选表（含每类图片数、已完成数）+ 数量 + 续跑/强制；运行中切换为进度视图 |
| 缺陷库 | 体检结果、按类型/严重度分布、安装数据包 |
| 导出 | 选择类别范围、预估体积与文件数、一键打包 |
| 设置 | 工作区、数据目录、`base_url`、API key、并发数、主题 |
| 日志 | 实时日志、级别过滤、打开日志目录、失败原因分类统计 |

概览页的设计意图是把 README 里“开跑前的三步自检”变成一屏可见的状态，而不是三条要背的命令。

### 9.2 运行中视图

进度条 + 四个计数块（合格 / 驳回 / 失败 / 已跳过）+ 速率与预计剩余时间 + 日志尾部 + 停止按钮。
预计剩余时间按已完成任务的移动平均单张耗时估算，明确标注为估算值。

### 9.3 视觉规范

- 深色为默认主题，提供浅色切换。主色调偏工业蓝青，语义色：成功绿、警告琥珀、错误红。
- 字体优先 `Microsoft YaHei UI`，回退 `Segoe UI`。
- 圆角 8px、卡片式分区、8px 基准间距栅格。
- 样式集中在 `resources/theme.qss`，颜色变量集中在 `theme.py`，不在控件代码里散落硬编码色值。
- 自定义控件：`Card`、`StatCard`、`StatusDot`、`LogView`、`ClassTable`。
- `LogView` 限制最大保留行数（默认 5000，滚动丢弃最旧），对应 NFR-3 长跑内存约束。

### 9.4 无障碍（NFR-6）

- 所有交互控件设置 `accessibleName` / `accessibleDescription`。
- 完整键盘可达，显式设定 Tab 顺序，焦点有可见描边。
- 正文文本与背景对比度不低于 4.5:1，大号文本不低于 3:1。
- 状态不只靠颜色传达：状态点始终配文字标签。
- 已知限制：完整符合性需人工配合屏幕阅读器验证，本设计只保证可测量项。

### 9.5 线程模型

GUI 主线程只做渲染。耗时操作（数据集扫描、缺陷库体检、API 测试、导出打包）放 `QThreadPool` 的 `QRunnable`，通过 `Signal` 回主线程。生成任务不在 GUI 进程内执行（见 7.2）。
界面只持有路径字符串与统计数值，不持有 `PIL.Image` 对象（对应风险表内存项）。

## 10. 打包（DD10）

### 10.1 双 exe 单 COLLECT

GUI 需要 `console=False`（否则弹黑框），CLI 需要控制台才能输出。PyInstaller 支持一个 `.spec` 产出多个 EXE 共享同一份 `COLLECT`：

| 产物 | 模式 | 入口 | 用途 |
|---|---|---|---|
| `DefectSynth.exe` | windowed | `app_gui.py` | 图形界面 |
| `defectsynth-cli.exe` | console | `run.py` | CLI 与 worker（`--worker`） |

GUI 启动后台任务时调用 `defectsynth-cli.exe --worker --job <dir>`。

采用 onedir 而非 onefile：onefile 每次启动要把约 200 MB 解压到临时目录，启动慢且长跑期间占用双份磁盘。

### 10.2 依赖调整

- `opencv-python` → `opencv-python-headless`。理由见需求 D1，依据 C10。
- 新增 `PySide6`、`keyring`。
- 全部锁定精确版本（FR-8.1）。

### 10.3 spec 关键配置

- `excludes`：`PySide6.QtWebEngineCore` `QtWebEngineWidgets` `QtQuick*` `Qt3D*` `QtMultimedia*` `QtCharts` `QtDataVisualization` `QtBluetooth` `QtNfc` `QtPositioning` `QtSql` `QtTest` `QtDesigner` `tkinter` `matplotlib` `scipy` `pandas` `PIL.ImageQt`
- `hiddenimports`：`keyring.backends.Windows`（keyring 后端是运行时动态发现的，不显式声明会在冻结后找不到）
- `datas`：`config.yaml`、`app/gui/resources/*`
- `runtime_hooks`：`runtime_hook_utf8.py`，设置标准流为 UTF-8（对应 C6、FR-7.2）

### 10.4 构建流程（`build.ps1`）

```
清理 build/ dist/
→ 校验依赖锁文件已安装
→ PyInstaller 打包
→ 冒烟：defectsynth-cli.exe selftest（验证 cv2/numpy 隐式导入完整）
→ 冒烟：DefectSynth.exe --version（验证 Qt 插件目录完整）
→ Inno Setup 生成安装包
→ 输出体积报告，超阈值告警
```

冒烟步骤写进构建脚本而非靠人工，因为“Qt 插件缺失”和“cv2 隐式导入缺失”这两类问题只在冻结后暴露，打包成功不代表能启动。

版本号单一来源 `app/version.py`，由 `build.ps1` 读出后注入 exe 版本资源与 Inno 脚本。

## 11. 契约汇总

新增的对外契约，实现时以此为准：

```
app.runtime.paths
    is_frozen() -> bool
    install_root() -> Path
    resource_path(rel: str) -> Path
    user_config_dir() -> Path
    workspace() -> Path                     # 可能抛 WorkspaceNotConfigured
    set_workspace(p: Path) -> None
    ensure_workspace_layout(p: Path) -> None

app.runtime.secrets
    get_api_key() -> str | None              # env -> 凭据管理器
    set_api_key(key: str) -> None
    clear_api_key() -> None
    mask(key: str) -> str

app.jobs.spec
    JobSpec(job_id, kind, classes, count, force, max_workers, workspace)
    JobStatus(state, pid, started_at, finished_at, stats)

app.jobs.store
    create_job(spec) -> Path
    list_jobs() -> list[JobStatus]
    read_events(job_dir, offset) -> tuple[list[dict], int]
    request_cancel(job_dir) -> None
    is_alive(status) -> bool

app.jobs.launcher
    launch(spec) -> JobStatus                # 分离进程

app.services.catalog_service
    inspect() -> CatalogReport
    install_pack(zip_path: Path) -> CatalogReport

app.services.dataset_service
    scan(clean_root: Path) -> DatasetReport  # 类别 -> 图片数，含修复建议

app.services.export_service
    estimate(classes) -> ExportEstimate
    export(classes, dest: Path, progress_cb) -> Path

app.services.api_service
    test_connectivity() -> ApiTestResult     # 复用 test-api 逻辑

src.reporting
    Reporter / ConsoleReporter
    set_reporter / get_reporter / info / warn / event / track

src.catalog_report
    build_report(cfg) -> CatalogReport
```

## 12. 分阶段交付

| 阶段 | 内容 | 验收 |
|---|---|---|
| A 基础层 | 路径、配置、密钥、日志、输出边界、取消支持 | 原有 11 个 CLI 子命令输出与改造前一致 |
| B 服务层 | 缺陷库体检/安装、数据集扫描、导出、连通性 | CLI 可驱动全部服务，结果与现有命令一致 |
| C 作业层 | JobSpec、事件流、分离进程、附着与取消 | 命令行启动作业，关闭终端后仍在跑，可取消 |
| D 界面层 | PySide6 全部页面与主题 | 完成需求第 6 节验收标准 1~5 |
| E 打包 | spec、构建脚本、安装包、冒烟 | 完成验收标准 7 |
| F 测试 | 推迟，待 D3 重新评估 | — |

阶段 A 结束时必须先完成一次完整回归，再进入 B。原因：A 动的是所有命令共用的路径与输出通道，是本次改造唯一可能破坏既有产出的地方。
