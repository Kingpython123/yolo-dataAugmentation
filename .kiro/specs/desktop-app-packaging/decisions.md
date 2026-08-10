# 实施期决策记录

记录实施过程中相对 `design.md` / `tasks.md` 的偏离, 以及为什么这样改。
每条都写明依据, 便于后续维护者判断是否可以改回去。

## D-01 事件流走文件, 不走 stdout 管道

`design.md` 7.2 原方案是 worker 把事件写 stdout, 界面读管道。实施时改为写
`作业目录/events.jsonl`, 界面按字节偏移增量读。

理由:

1. **管道背压会造成假死。** 界面随时可能被关闭。管道一旦无人读取, worker 写满
   缓冲区就会阻塞在 write 上, 表现为"任务莫名卡住", 而且从日志里完全看不出原因。
   文件写入没有背压。
2. **重新附着变成免费的。** 界面重开后直接从文件头读一遍就能还原全部历史进度,
   不需要任何额外机制。走管道则必须再设计一套快照或重放。

连带取消的措施: `design.md` 里"把 sys.stdout 替换为转发到 stderr 的包装, 防止裸
print 污染协议流"不再需要 —— 协议流已经是独立文件, 遗漏的 print 落到 worker.log
里反而有利于排查。

## D-02 CLI 用 `worker` 子命令而非 `--worker` 全局开关

`design.md` 10.1 写的是 `defectsynth-cli.exe --worker --job <dir>`。

现有 CLI 已经是 11 个子命令的结构, 且 `sub.add_subparsers(required=True)` 与全局
开关混用时, 用户只敲 `--worker` 会得到"缺少子命令"这种与实际问题无关的报错。改成
子命令后与既有风格一致, 报错也直观。

同时新增 `job-start` / `job-list` / `job-cancel`, 使作业能力在没有界面时也完整可用
(便于脚本化与排障)。

## D-03 缺陷库仍位于 `工作区/outputs/catalog`

没有迁到 `工作区/catalog`。

`config.yaml` 的 `output.catalog` 现值就是 `outputs/catalog`, 而 `Config.resolve()`
按"相对 config.yaml 所在目录"解析。工作区里放着 config.yaml, 所以这个相对路径天然
指向工作区内部。不改路径即不改解析语义, 也保住了 `annotations.jsonl` 里记录的路径
字符串 —— 断点续跑靠它推导 stem(约束 C8), 动了就会让所有人的历史进度失效。

## D-04 `defect_catalog.py` 与 `requalify.py` 的 print 未迁移到输出边界

`tasks.md` 任务 6 原计划一并迁移。

- `defect_catalog.py` 的输出全部在 `build_catalog` 路径上, 而重建缺陷库是需求里
  明确的非目标(N2), 界面不暴露该功能。
- `requalify.py` 是纯控制台报表格式化(带列对齐), 角色等同于 `run.py` 里的
  `cmd_*` 处理器, 而设计文档已明确豁免后者。

两者在本次范围内都没有界面消费方, 迁移只会增加破坏"行为等价"(NFR-1)的风险而无
收益。若将来要把它们接入界面, 迁移是机械的, 届时再做。

## D-05 界面工具选 PySide6 而非 Tkinter

需求 D1 最初倾向 Tkinter(零新增依赖、体积最小), 在明确"观感要现代"后改为 PySide6。

连带的硬约束: `opencv-python` 必须换成 `opencv-python-headless`。前者自带一套 Qt5
插件, 与 PySide6 的 Qt6 在冻结后会争抢 `platforms/qwindows.dll`。已核实项目只用
cv2 的算法函数(无 `imshow` / `waitKey` / `VideoCapture`), 替换无功能影响; 且替换后
`selftest` 的分割前景占比 0.281、掩膜外变化 0.00000 与 README 记录值完全一致。

## D-06 opencv 锁在 4.x

`requirements.txt` 原本写 `opencv-python>=4.8`。实际执行 pip 安装时默认装到了
OpenCV **5.0.0.93** —— 那是大版本跳跃, 形态学/阈值等算子的数值行为可能改变, 与
NFR-1"行为等价"直接冲突。因此在 `requirements/runtime.txt` 里锁 `<5`。

## D-07 GUI 后台任务必须关掉 QRunnable 的 autoDelete

实施中发现的真实缺陷, 记录在此以免被"简化"回去。

`QRunnable` 默认 `autoDelete=True`: `run()` 返回后线程池立即删除它。而承载信号的
`QObject` 是这个 Python 对象的属性, 调用方通常不保存 `submit()` 的返回值 —— Python
侧失去最后一个引用, 对象被回收, 已排队等待投递到主线程的信号就此丢失。

症状是回调时而触发时而不触发。离屏截图时表现为"导出页的类别表是空的、预估全是
`-`", 而同一份扫描逻辑在概览页却正常。

修法: `setAutoDelete(False)` + 模块级强引用集合, 待 `done` 信号送达主线程后再移除。
释放引用的连接必须最后挂, 因为槽按连接顺序执行。

## D-08 QSS 中标签类控件必须显式透明

`QWidget { background: ... }` 会连带作用到 `QLabel` / `QCheckBox`, 使它们在卡片上
画出更深的窗口底色, 形成一条条横条。表格里包装复选框用的 `QWidget` 容器同理, 会在
复选框旁多出一个空方框。

因此 QSS 里显式给 `QLabel, QCheckBox, QRadioButton, QGroupBox` 设透明, 复选框容器
单独设 `background: transparent`。

## D-09 样式表设在 QApplication 上, 不设在主窗口上

`QMessageBox` / `QFileDialog` 是独立的顶层窗口。只给主窗口上样式时它们会保持系统
默认外观, 在深色主题下显得很突兀。

## D-10 不裁剪 QSpinBox 的上下按钮样式

一旦覆盖 `QSpinBox::up-button` 的 `background`, Qt 就不再绘制原生箭头图形, 只剩一条
空白灰条。保留原生绘制。

## D-11 打包时剔除 opencv 自带的 FFmpeg DLL

首次构建实测 `opencv_videoio_ffmpeg4140_64.dll` 占 29.1 MB, 是整个发行版里第二大的
文件。它只服务 `cv2.VideoCapture` / `VideoWriter`, 项目一个都没用, 因此在 `app.spec`
里按文件名前缀剔除。纯体积收益。

## D-12 带中文的 `.ps1` 必须有 UTF-8 BOM

Windows PowerShell 5.1 在脚本没有 BOM 时按系统 ANSI 代码页(本机为 GBK)解码 `.ps1`。
UTF-8 编码的中文注释因此被解成乱码, 破坏字符串字面量并导致
`表达式或语句中包含意外的标记"}"` 这类与实际语法无关的解析错误。

`tools/add_ps1_bom.ps1` 负责给含非 ASCII 字符的 `.ps1` 补 BOM; 纯 ASCII 脚本保持无
BOM。

## 本机工具链注意事项(与项目本身无关, 但影响后续维护)

- 编辑器的字符串替换工具会把 UTF-8 中文源文件按 GBK 往返一次, 造成整文件乱码。
  修改含中文的既有文件请用一次性 Python 迁移脚本, 显式 `encoding="utf-8"` 且
  `newline=""` 以保留 CRLF。
- 文件写入工具的编码不稳定(时 UTF-8 时 GBK)。写完统一跑
  `& .\tools\normalize_text.ps1` 规范为 UTF-8 + CRLF。
