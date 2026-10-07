# CodeNova

> 面向 Python 依赖升级的 Coding Agent：分析影响、自动改写、验证结果，并为高风险变更提供可追踪的语义修复流程。

CodeNova 将可重复的确定性 Codemod 与可选的 LLM Agent 结合起来，面向整个 Python 仓库完成依赖升级。它不只修改版本号，还会扫描 Python AST 与依赖关系、迁移受影响的 API、执行测试和静态检查，并输出可审查、可恢复的迁移产物。

当前内置了较完整的 **Pydantic v1 → v2** 规则集；其他 Python 包也可以使用通用的依赖更新、仓库扫描、验证和语义任务流程。

## 核心能力

- **仓库级分析**：扫描依赖清单和 Python AST，建立保守的文件/符号依赖图。
- **确定性迁移**：只对能够静态证明安全的代码执行保留源码格式的局部 Codemod。
- **语义修复**：将无法安全机械处理的问题转成结构化任务，可交给配置好的 LLM Agent。
- **闭环验证**：自动发现并运行 pytest、lint、mypy、pyright 等检查，也支持显式指定命令。
- **风险控制**：高风险问题未解决时返回 `needs-review`，不会把不确定的修改伪装成成功。
- **可恢复执行**：保存备份、补丁、哈希链日志、原子检查点和机器可读结果，支持续跑与回滚。
- **量化评估**：记录规则与 Agent 修改数、问题消除率、验证通过率、耗时、Token 和成本估算。

## 工作流程

```text
依赖清单 + Python 源码
          │
          ▼
   AST / import 扫描
          │
          ▼
  文件与符号依赖图
          │
     ┌────┴────┐
     ▼         ▼
安全 Codemod   高风险语义任务
     │         │
     │    LLM Agent / 人工审查
     └────┬────┘
          ▼
 测试 / lint / 类型检查
          │
          ▼
 报告、Patch、备份与 Checkpoint
```

## 环境要求

- Python 3.11+
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境
- 只有启用 `--agent` 或交互式 Coding Agent 时才需要配置模型服务

## 安装

克隆仓库后，在项目根目录执行：

```bash
uv sync --dev
```

也可以使用 pip 进行可编辑安装：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

安装完成后使用 `codenova` 命令运行项目。

## 快速开始

先用 dry-run 查看拟议改动，不修改目标项目源码：

```bash
uv run codenova migrate \
  --package pydantic \
  --from 1.10 \
  --to 2.8 \
  --project /path/to/your-project \
  --dry-run
```

确认后执行迁移：

```bash
uv run codenova migrate \
  --package pydantic \
  --from 1.10 \
  --to 2.8 \
  --project /path/to/your-project
```

显式指定验证命令：

```bash
uv run codenova migrate \
  --package pydantic \
  --from 1.10 \
  --to 2.8 \
  --project /path/to/your-project \
  --check "pytest -q" \
  --check "mypy src"
```

## Pydantic v1 → v2 支持

内置规则覆盖以下常见场景：

- `BaseSettings` 迁移至 `pydantic-settings`
- `Config` 迁移至 `ConfigDict`
- `validator` / 前置 `root_validator` 迁移
- 可静态确认的 `parse_obj`、`dict`、`json`、`schema`、`from_orm` 等 API 更新
- `Optional` 字段默认值变化
- Pydantic 泛型模型基类调整
- 依赖约束与 `pydantic-settings` 配套依赖更新

涉及复杂 `Config`、动态模型类型、后置 root validator 或业务语义的代码会被标记为高风险任务，而不是直接猜测修改。

## 使用 LLM Agent 处理语义问题

在项目目录或用户目录创建 `.codenova/config.yaml`：

```yaml
providers:
  - name: openai
    protocol: openai
    base_url: https://api.openai.com/v1
    model: YOUR_MODEL

permission_mode: default
```

通过环境变量提供密钥：

```bash
export OPENAI_API_KEY="YOUR_API_KEY"
```

然后启用 Agent 修复，并限制最大迭代次数：

```bash
uv run codenova migrate \
  --package pydantic \
  --from 1.10 \
  --to 2.8 \
  --project /path/to/your-project \
  --agent \
  --agent-iterations 3
```

支持的协议为 `anthropic`、`openai` 和 `openai-compat`。请勿将真实 API Key 提交到仓库。

## 迁移状态

| 状态 | 含义 |
|---|---|
| `completed` | 最终验证通过，且没有未解决的高风险问题 |
| `dry-run` | 已完成分析并生成报告，没有修改目标源码 |
| `needs-review` | 仍有高风险问题需要 Agent 或人工处理 |
| `validation-failed` | 最终验证批次失败 |

`needs-review` 和 `validation-failed` 会返回非零退出码，便于接入 CI。

## 迁移产物与恢复

每次运行的产物保存在目标项目的 `.codenova/migrations/<run-id>/`：

```text
.codenova/migrations/<run-id>/
├── backups/             # 原文件备份
├── changes.patch        # 可审查的统一 diff
├── checkpoint.json      # 原子检查点
├── journal.jsonl        # 哈希链执行日志
├── result.json          # 机器可读结果与指标
├── report.md            # Markdown 迁移报告
└── semantic-input.json  # 需要语义处理的结构化任务
```

续跑最近一次中断的任务：

```bash
uv run codenova migrate \
  --package pydantic --from 1.10 --to 2.8 \
  --project /path/to/your-project --resume
```

回滚某次迁移中的指定文件：

```bash
uv run codenova migrate \
  --project /path/to/your-project \
  --rollback RUN_ID \
  --file src/models.py
```

## 量化实验

迁移结果中的 `metrics` 包含自动修改数、风险问题清除率、验证通过率、阶段耗时、Agent 迭代次数、Token 用量和可选成本估算。

可以汇总同一批仓库上的纯规则与混合 Agent 实验：

```bash
uv run codenova benchmark \
  --run rules:case-a=/path/to/rules/result.json \
  --run hybrid:case-a=/path/to/hybrid/result.json \
  --baseline rules \
  --output-dir benchmarks/results/comparison
```

## 交互模式

CodeNova 也保留了终端 Coding Agent、非交互模式和远程 Web 界面，可用于处理依赖升级中的语义长尾问题：

```bash
# TUI
uv run codenova

# 非交互调用
uv run codenova -p "检查这个仓库升级 Pydantic v2 的剩余风险"

# 本地远程界面（默认端口 18888）
uv run codenova --remote
```

## 项目结构

```text
codenova/
├── migration/       # 扫描、规则、迁移编排、验证、报告与 benchmark
├── runtime/         # 资源感知调度、WAL、Checkpoint 与结果重放
├── permissions/     # 工具权限与危险操作检查
├── tools/           # 文件、Shell、Agent 与任务工具
├── agents/          # 子 Agent 定义、加载与追踪
├── teams/           # 多 Agent 协作
├── memory/          # 指令与记忆系统
└── mcp/             # MCP 客户端与工具封装
```

Python 导入包、命令行入口和发行包统一使用 `codenova`，运行数据与项目配置统一保存在 `.codenova/`。

## 开发与测试

运行完整测试：

```bash
uv run pytest
```

运行迁移与可恢复 Runtime 的重点测试：

```bash
uv run pytest \
  tests/test_migration.py \
  tests/test_runtime_journal.py \
  tests/test_runtime_scheduler.py
```

运行不调用外部 API 的 Runtime 微基准：

```bash
uv run python benchmarks/runtime_benchmark.py
```

## 安全提示

- 首次迁移建议先使用 `--dry-run` 并审查 `changes.patch`。
- 在版本控制干净的工作区中运行自动迁移。
- 不要把 `.codenova/config.local.yaml`、API Key 或目标项目的迁移运行目录提交到公共仓库。
- 自动化结果不能替代针对业务行为的人工审查和集成测试。
