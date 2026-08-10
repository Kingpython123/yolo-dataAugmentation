# 实施计划：缺陷合成流水线桌面化封装

对应 `requirements.md` 与 `design.md`。实施期的偏离与理由记录在 `decisions.md`。

---

## 阶段 A：运行时基础层

- [x] 1. 建立冻结感知的路径体系
  - `app/runtime/paths.py`: `is_frozen` / `install_root` / `resource_path` /
    `user_config_dir` / `workspace` / `set_workspace` / `ensure_workspace_layout`
  - `workspace()` 四级解析: 环境变量 → settings.json → 源码回退仓库根 → 冻结报错
  - _需求：FR-2.1、FR-2.2、8.1~8.4；约束：C1_

- [x] 2. 用户配置持久化
  - `app/runtime/settings.py`，JSON 存于 `%APPDATA%\DefectSynth\settings.json`
  - 明确不存 API key
  - _需求：FR-3.1、8.3_

- [x] 3. 密钥存储
  - `app/runtime/secrets.py`，`keyring` + Windows 凭据管理器
  - 读取顺序 `RELAY_API_KEY` → 凭据管理器；`mask()` 只留后 4 位；`redact()` 用于日志
  - _需求：FR-3.2、FR-3.3、FR-3.4、NFR-2_

- [x] 4. 标准流编码与日志
  - `app/runtime/encoding.py`、`app/runtime/logging_setup.py`（轮转 + 密钥脱敏 Filter）
  - _需求：FR-5.6、FR-7.2、NFR-2、NFR-3；约束：C6_

- [x] 5. 核心层输出边界
  - `src/reporting.py`: `Reporter` 协议、`ConsoleReporter`、`info/warn/event/track/progress_tick`
  - _需求：NFR-4；约束：C3_

- [x] 6. 迁移核心层的 print 与 tqdm
  - `src/generate.py` 全部迁移（28 处定向改动，0 残留 print）
  - `defect_catalog.py` / `requalify.py` 刻意不迁移，理由见 decisions.md D-04
  - _需求：FR-2.3、NFR-4_

- [x] 7. 在 `_run_tasks` 中发出结构化事件
  - `plan` / `task` / `progress` / `stats` / `finished`
  - _需求：FR-5.2、FR-5.5、NFR-5_

- [x] 8. 配置层支持工作区
  - `src/config.py` 改为依赖倒置：`set_default_config_path_provider` 挂钩，
    未注入时行为与改造前完全一致
  - `Config.resolve` / `out_path` 未改
  - _需求：FR-2.2、FR-3.1；约束：C2、C8_

- [x] 9. 优雅取消支持
  - `_run_tasks(cancel=Event)`；未启动任务立即返回 `CANCELLED` 哨兵，不发起 API 调用
  - 取消计入 `stats["cancelled"]`，不计 failed，不写 `fail_log.jsonl`
  - _需求：FR-5.4_

- [x] 10. 阶段 A 回归
  - `selftest` / `inspect` / `fail-summary` / `requalify` 输出与 `baseline/` 逐行一致
  - _需求：FR-2.3、验收标准 6；NFR-1_

---

## 阶段 B：业务服务层

- [x] 11. 提炼缺陷库体检
  - `src/catalog_report.py`: `build_report()` + `format_lines()`；`cmd_inspect` 输出不变
  - _需求：FR-4.1_

- [x] 12. 数据集扫描服务
  - `app/services/dataset_service.py`，含半角/全角括号、层级错误、扩展名不匹配的诊断建议
  - _需求：FR-4.2、FR-4.3_

- [x] 13. 缺陷库数据包制作工具
  - `packaging/make_catalog_pack.py`；实测归一化了 540/668 条绝对路径
  - _需求：FR-4.4；约束：C5_

- [x] 14. 缺陷库安装服务
  - `app/services/catalog_service.py`：manifest 校验 → 临时目录解压 → 逐文件
    SHA-256 复验 → 整体替换
  - _需求：FR-4.1、D2_

- [x] 15. 连通性测试服务
  - `app/services/api_service.py`，复用 `test-api` 两步验证
  - _需求：FR-3.5_

- [x] 16. 成果导出服务
  - `app/services/export_service.py`：`estimate` / `export`，硬编码排除 debug 类目录
  - _需求：FR-6.1、FR-6.2、FR-6.3_

---

## 阶段 C：作业层

- [x] 17. 作业规格与事件协议
  - `app/jobs/spec.py`、`app/jobs/protocol.py`（`JobProgress` 折叠事件为进度）
  - _需求：FR-5.1_

- [x] 18. 作业目录读写
  - `app/jobs/store.py`：字节偏移增量读事件、容忍半行；进程存活用
    `OpenProcess`+`GetExitCodeProcess` 结合心跳
  - _需求：FR-5.2、FR-5.4、FR-5.8_

- [x] 19. worker 进程入口
  - `app/worker.py`：`JsonlReporter` + `LoggingReporter` + 统计截获，轮询 cancel.flag，
    异常时补发 `finished` 事件
  - _需求：FR-5.2、FR-5.4、FR-5.6_

- [x] 20. 分离进程启动器
  - `app/jobs/launcher.py`：`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`
  - _需求：FR-5.8_

- [x] 21. CLI 暴露作业能力
  - `run.py` 新增 `worker` / `job-start` / `job-list` / `job-cancel`
  - _需求：FR-7.1、FR-5.8_

---

## 阶段 D：界面层

- [x] 22. 主题与基础控件
  - `app/gui/theme.py` + QSS 模板（深/浅两套），`widgets.py`:
    `Card` / `StatCard` / `StatusDot` / `Banner` / `LogView`(限 5000 行) /
    `ClassTable` / `LabeledProgress`
  - 全部交互控件设 `accessibleName`，状态色 + 文字双通道
  - _需求：NFR-6、NFR-3、D1_

- [x] 23. 主窗口与导航
  - `app_gui.py` + `app/gui/main_window.py`；侧边导航 6 项、Ctrl+1..6 快捷键、F5 刷新
  - 首次启动工作区引导，展示磁盘可用空间并在低于 20 GB 时确认
  - _需求：8.3、FR-1.4_

- [x] 24. 概览页 —— 四张状态卡片（工作区 / 缺陷库 / 数据集 / 连通性）
  - _需求：FR-3.5、FR-4.1、FR-4.3_

- [x] 25. 设置页 —— 工作区、数据目录、base_url、掩码 key、并发、主题
  - _需求：FR-3.1、FR-3.3、FR-3.5、FR-4.2_

- [x] 26. 缺陷库页 —— 体检结果、分布统计、数据包安装
  - _需求：FR-4.1、D2_

- [x] 27. 生成页 —— 类别多选、preflight、进度视图、优雅停止、自动附着
  - _需求：FR-5.1~FR-5.5、FR-5.8_

- [x] 28. 日志页 —— 作业历史、失败原因分类、目录入口
  - _需求：FR-5.3、FR-5.6、FR-5.7_

- [x] 29. 导出页 —— 范围选择、体积预估、打包
  - _需求：FR-6.1~FR-6.3_

- [x] 30. 阶段 D 验收（离屏渲染 12 张截图 + 无障碍属性检查通过）
  - _需求：验收标准 2~5_

---

## 阶段 E：打包与分发

- [x] 31. 依赖锁定与替换
  - `requirements/runtime.txt`、`build.txt` 全部精确版本
  - `opencv-python` → `opencv-python-headless`，并锁 `<5`（见 decisions.md D-06）
  - _需求：FR-8.1；约束：C10_

- [x] 32. PyInstaller 配置
  - `packaging/app.spec`：双 exe 单 COLLECT、onedir、Qt 模块裁剪、
    `keyring.backends.Windows` 显式声明、UTF-8 运行时钩子、剔除 FFmpeg DLL
  - _需求：FR-1.2、FR-1.3、FR-7.2_

- [x] 33. 构建脚本
  - `packaging/build.ps1`：依赖自检 → 清理 → 打包 → 冒烟（selftest 数值比对 +
    GUI 离屏启动）→ Inno Setup → 体积报告
  - _需求：FR-8.2、FR-8.3、FR-1.4_

- [x] 34. 安装包脚本
  - `packaging/installer.iss`：中文向导、快捷方式、不预设工作区、卸载不动工作区
  - _需求：FR-1.1_

- [ ] 35. 分发验证（**未完成**）
  - 已完成：本机构建成功，冻结版 `selftest` 数值与基线一致，GUI 可启动，
    安装后占用 234.1 MB（< 300 MB 上限）
  - 未完成：安装包未实际生成（本机没装 Inno Setup 6）；未在无 Python 的干净机器上
    走完"安装 → 引导 → 装缺陷库 → 生成 10 张 → 导出"全流程
  - 阻塞原因：缺 Inno Setup、缺数据集与 API key
  - _需求：验收标准 1、7；FR-1.3_

---

## 阶段 F：测试（推迟）

- [ ] 36. 重新评估 D3
  - 现状：已有三套可重复执行的验证脚本（见下），但不是单元测试
  - 建议范围：`mask_utils` 纯函数、配置分层、冻结路径解析、事件协议折叠
  - _需求：D3、TD-1、NFR-1_

---

## 验证脚本

这些脚本可随时重跑，是当前"行为等价"的实际保障：

| 脚本 | 验证内容 | 是否调用真实 API |
|---|---|---|
| `tools/check_boundary.py` | 事件齐全 / 优雅取消不写 fail_log 且 annotations 不损坏 / ConsoleReporter 静默事件 | 否（假客户端） |
| `tools/check_jobs.py` | 真实分离进程跑完整链路：正常完成、优雅取消、缺陷库缺失的异常收尾 | 否（本地 HTTP 桩） |
| `tools/check_gui.py` | 离屏渲染 6 个页面 × 2 套主题，并检查无障碍属性 | 否 |

基线在 `baseline/`，回归方式：`run.py <cmd>` 输出与之逐行 `Compare-Object`。
