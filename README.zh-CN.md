<div align="center">

[English](README.md) | [**中文**](README.zh-CN.md) | [Русский](README.ru.md)

# ZipSearch

**无需批量解压，即可在 ZIP 归档中搜索和检查文本与结构化记录。**

</div>

ZipSearch 是本地终端工具，用于检索 ZIP 文件中保存的异构数据：导出文件、日志、电子表格、SQLite 数据库和嵌套归档。它直接读取归档成员，以受控的资源使用方式提供命令行与 curses TUI 两种检查界面，而不要求先建立长期保留的解压目录树。

ZipSearch 可以看作 `awerpars` 的精神续作：那个更早的项目所积累的一些思路和经验，后来演化为这个独立实现。

## 为什么需要它

大批量归档并不适合手工检查。为了查找一个值而解压全部归档，会重复占用存储、增加 I/O，并留下清理工作。ZipSearch 会发现 ZIP、读取符合条件的成员，并通过 CLI 和 TUI 展示结果。

> ZipSearch 在读取时仍会解压数据。“无需批量解压”指不会把归档集合展开为持久目录树。部分处理器会使用自动清理的临时存储，详见[存储行为](#存储行为)。

## 概览

| 方面 | ZipSearch 提供的能力 |
| --- | --- |
| 搜索 | LITERAL、正则表达式与归一化、按 token 感知的 SMART 搜索 |
| 数据 | 文本类成员、SQLite 行、XLSX 工作表行、嵌套 ZIP |
| 检查 | 归档 → 成员 → 结果树、DETAIL 面板、中央目录检查 |
| 操作 | include/exclude glob、扩展名过滤、上下文、切换根目录、导出、取消 |
| 控制 | 成员数、大小、展开后总大小、压缩比、嵌套深度限制；可恢复问题报告 |

## 安装

需要 Python **3.10 或更高版本**。ZipSearch 没有运行时第三方依赖。

```console
python -m pip install .
zipsearch --version
```

在源码检出目录中进行开发：

```console
python -m pip install -e . pytest ruff
```

不带参数运行 `zipsearch` 时，只有标准输入和输出均为终端才会打开 TUI。在脚本中请显式使用子命令。

## 快速开始

```console
# 交互式 TUI；根目录可以是目录或单个 ZIP
zipsearch tui ./archives

# 普通 LITERAL 搜索（默认忽略大小写）
zipsearch search ./archives 'customer@example.com'

# SMART：适合人员/电话等查询，感知标点、大小写和词序
zipsearch search ./archives 'Глеб Скрепкин +79087562342' --smart

# 正则、上下文行和 JSON Lines 输出
zipsearch search ./archives 'INV-[0-9]{8}' --regex -C 2 --jsonl > matches.jsonl

# 限定成员路径与扩展名
zipsearch search ./archives needle --extension csv --extension json \
  --include 'exports/**' --exclude '*backup*'

# 仅读取归档中央目录，不解压成员
zipsearch inspect evidence.zip --json
```

完整 CLI 参数请运行 `zipsearch search --help`。

## 搜索语义

| 模式 | 命中规则 | 排序 |
| --- | --- | --- |
| **LITERAL** | 每个 pattern 会转义后执行连续正则搜索；多词顺序有意义。 | 归档/成员/行扫描顺序，受全局上限约束。 |
| **REGEX** | 每个 pattern 按 Python 正则表达式编译。 | 归档/成员/行扫描顺序，受全局上限约束。 |
| **SMART** | 对每个查询 pattern 考虑归一化短语、token、前缀和电话证据。 | 证据更强的结果在全局范围靠前。 |

CLI 默认 LITERAL；TUI 默认 SMART。`--smart` 与 `--regex` 不能同时使用。除非使用 `--case-sensitive`，搜索默认忽略大小写；`--ignore-case` 可显式指定默认行为。

### SMART 匹配

SMART 采用 NFKC 归一化；在忽略大小写时会折叠大小写，并将俄语 `ё` 视为 `е`；标点会作为分隔符，连续空白会合并。它不使用编辑距离，也不会猜测拼写。

对于多 token 查询，SMART 依次优先：归一化短语命中、任意词序的全 token 命中、安全的 token 前缀命中，以及有用的部分 token 命中。因此，多词查询中只有一部分命中时，记录仍可能保留。电话号码形态的查询组件若至少含七位数字，会忽略分隔符；俄罗斯 `8xxxxxxxxxx` 会归一化为 `7xxxxxxxxxx`。

例如，`Глеб Скрепкин +79087562342` 可以命中 `Игорь Скрепкин; +7 (908) 756-23-42`。其中的命中证据是 `Скрепкин` 和电话号码；`Глеб` 仍显示在查询中，但不被当作证据。

`match_type` 和 `score` 会出现在 JSONL 输出及结果详情中。score 仅是内部排序信号，不是百分比，也不能跨查询比较相关性。

## TUI

`zipsearch tui [PATH]` 打开键盘优先的界面，其中包含可编辑查询行、RESULTS 面板、DETAIL 面板以及紧凑的状态/底部区域。

- **RESULTS** 是树：归档 → 成员 → 命中记录。折叠归档或成员不会改变已加载的结果、排名或搜索本身。
- **DETAIL** 显示所选记录，包括完整原始 `matched` 查询，以及实际促成该结果的查询组件；选中树节点时还显示已有的归档/成员元数据。
- 命中证据会加下划线；curses 支持颜色时可使用一种克制的强调色。单色终端仍使用属性高亮，选中行保持反显。
- 状态行显示归档/成员计数、命中数、问题数、worker 数、嵌套深度、**S**（当前或上次搜索时长）和 **U**（持续增长的 TUI 运行时间）。

### 键盘参考

| 按键 | 操作 |
| --- | --- |
| <kbd>/</kbd> | 聚焦/编辑查询 |
| <kbd>Enter</kbd> | 执行查询；浏览时检查所选树节点或结果 |
| <kbd>↑</kbd>/<kbd>↓</kbd>、<kbd>j</kbd>/<kbd>k</kbd> | 浏览结果；编辑时浏览查询历史 |
| <kbd>PgUp</kbd>/<kbd>PgDn</kbd>、<kbd>Home</kbd>/<kbd>End</kbd> | 浏览结果 |
| <kbd>Space</kbd> | 切换所选归档/成员节点的展开状态 |
| <kbd>←</kbd> / <kbd>→</kbd> | 折叠 / 展开所选归档/成员节点 |
| <kbd>r</kbd> | 更改根目录（目录或 `.zip`） |
| <kbd>m</kbd> | SMART → LITERAL → REGEX |
| <kbd>c</kbd> | 切换大小写敏感 |
| <kbd>f</kbd> | 打开过滤器和限制设置 |
| <kbd>x</kbd> | 查看可恢复问题 |
| <kbd>e</kbd> | 将当前结果导出为 JSONL、CSV 和文本文件 |
| <kbd>Ctrl-C</kbd> | 取消活动搜索 |
| <kbd>?</kbd> / <kbd>q</kbd> | 帮助 / 退出 |

TUI 中以逗号分隔的查询组件会成为独立 pattern。切换根目录会保留查询和设置，但清除过期结果。查询历史在会话中累积；浏览到最新条目之后会恢复当前正在编辑的草稿。

## 数据与归档支持

| 类别 | 支持的输入 | 处理方式 |
| --- | --- | --- |
| 容器 | `.zip` | 可直接指定归档，或递归发现目录中的 ZIP；支持配置深度内的嵌套 ZIP |
| 文本类成员 | `.txt`, `.csv`, `.tsv`, `.log`, `.json`, `.jsonl`, `.xml`, `.html`, `.htm`, `.md`, `.rst`, `.yaml`, `.yml`, `.ini`, `.cfg`, `.conf`, `.dat`, `.tad`, `.cpy` | 按行解码和搜索 |
| SQLite | `.db`, `.sqlite`, `.sqlite3` | 只读表行转为可搜索记录 |
| XLSX | `.xlsx` | 工作表 XML 行和 shared strings 转为可搜索记录 |

默认会跳过上述扩展名之外的成员。`--extension EXT` 可显式选择扩展名；扩展名、include 和 exclude 过滤都针对成员路径。文本解码 `auto` 先尝试 UTF-8，失败后使用 CP1251；已知编码时请传入 `--encoding CODEC`。文本初始探测中出现 NUL 字节时，成员会按二进制跳过。

CLI 中嵌套 ZIP 路径用 `!` 表示，例如 `outer.zip:inner.zip!records.txt:42`。

## CLI 选项与限制

### 常用搜索控制

| 选项 | 默认值 | 含义 |
| --- | ---: | --- |
| `--max-matches` | `1000` | 全局输出/结果上限 |
| `-C`, `--context` | `0` | 文本记录命中前后显示的行数 |
| `--encoding` | `auto` | `auto` 或任意 Python codec 名称 |
| `-j`, `--workers` | `4` | 归档 worker 线程数 |
| `--nested-depth` | `2` | 嵌套 ZIP 深度；`0` 禁用嵌套 ZIP 遍历 |
| `--include GLOB` | — | 要求成员路径或 basename 匹配；可重复 |
| `--exclude GLOB` | — | 跳过匹配的成员路径或 basename；可重复 |
| `--extension EXT` | — | 仅扫描选定扩展名；可重复 |
| `--no-recursive` | off | 不进入输入目录的下级目录 |
| `--jsonl` | off | 以 JSON Lines 写出匹配对象和最终摘要 |
| `-q`, `--quiet` | off | 不输出人工摘要和警告 |

### 资源限制

| 选项 | 默认值 | 在解压成员前检查 |
| --- | ---: | --- |
| `--max-member-mib` | `512` MiB | 单个成员声明大小 |
| `--max-total-mib` | `2048` MiB | 一个归档声明的展开后总大小 |
| `--max-members` | `100000` | 单个归档中的条目数 |
| `--max-ratio` | `200.0` | 单个成员声明的压缩比 |

TUI 也提供等价设置：扩展名、include/exclude glob、编码、上下文、嵌套深度、workers、上限及相同的安全限制。

## 输出、导出与错误处理

人工 CLI 输出格式为 `archive:member:line: text`；上下文行使用 `-` 和 `+`。`--jsonl` 对每个命中输出一个 JSON 对象，最后输出一个摘要对象。人工诊断和摘要写入标准错误。TUI 的导出命令会在当前工作目录写入带时间戳的 `.jsonl`、`.csv` 和 `.txt` 文件。

损坏归档、不可读/不支持成员、解码或解析失败、不安全路径、加密成员以及超限情况会在可能时作为可恢复问题报告；其他归档会继续扫描。CLI 退出码：无问题为 `0`，出现可恢复问题为 `1`，配置无效为 `2`，Ctrl-C 后为 `130`。

## 存储行为

ZipSearch 从不调用 `extractall`。普通文本成员直接从归档打开并增量读取。SQLite 需要随机访问，因此仅该成员会复制到自动删除的临时目录并以只读方式打开。XLSX 和嵌套 ZIP 使用 spooled 临时文件：前 8 MiB 保留在内存中，之后使用系统临时区域。`inspect` 显示的归档元数据来自中央目录，不会解压成员。

## 工作方式

```text
根路径 → 确定性 ZIP 发现 → 有界归档 workers
      → 中央目录安全检查 → 成员解码器/解析器
      → LITERAL / REGEX / SMART 匹配与证据归因
      → Match 记录 → CLI、JSONL 或 TUI 树/详情/导出
```

目录发现不会跟随目录符号链接。归档扫描使用固定大小线程池，最多保留两个 worker 窗口的待处理任务。SMART 结果通过有界 top-N 过程保留和排序，较晚出现的强命中可以替换较弱命中。

## 性能

速度取决于归档数量、压缩及展开后大小、成员格式、存储延迟、查询模式、过滤器、压缩比和 worker 数量。仓库提供一个可复现的 smoke benchmark；它不是性能宣称：

```console
python benchmarks/benchmark.py
```

如需通过公开 CLI 运行生成的功能数据集：

```console
python benchmarks/validate_realistic_dataset.py
```

<details>
<summary>运行建议</summary>

使用 extension/include/exclude 过滤器可避免解码无关成员。在合适的存储上，提高 `--workers` 可能有助于处理大量归档，但也会增加并发 I/O 与解压。提高大小或压缩比限制意味着你需要信任输入集合。

</details>

## 限制

- 仅支持 ZIP 容器。
- 加密 ZIP 成员会被跳过；未实现密码输入。
- XLSX 处理读取工作表值，不会像电子表格应用一样处理格式或公式。
- SQLite 和 XLSX 处理会按上述说明创建临时数据。
- 文本自动检测仅限 UTF-8 后备 CP1251；其他已知编码请使用 `--encoding`。
- 安全检查可降低恶意归档的风险，但不能代替对不可信输入进行操作系统级隔离。

## 开发

```console
python -m pip install -e . pytest ruff build
pytest
ruff check .
python -m build
```

持续集成在 Python 3.10、3.11 和 3.12 上运行测试与 Ruff。贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 仓库结构

```text
src/zipsearch/        包：引擎、搜索模式、CLI、渲染器、TUI
tests/                引擎、CLI、控制器和 PTY TUI 测试
benchmarks/           可复现 smoke 与生成数据集检查
CONTRIBUTING.md       贡献说明
LICENSE               MIT 许可证
```

## 许可证

采用 [MIT License](LICENSE) 发布。
