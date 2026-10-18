# 上游同步机制

本项目追踪上游 [earendil-works/pi](https://github.com/earendil-works/pi)。

当前对齐版本见根目录 [`UPSTREAM_VERSION`](./UPSTREAM_VERSION) —— 它是"已同步到上游哪个版本"的**唯一事实源**。

---

## 同步节奏

**每 2 周一次集中同步；期间若扫描器命中「破坏性变更」，提前处理。**

不逐个 release 跟进——上游 1.x 实测 **4.00 release/周**（间隔中位 1.87 天），逐个追不现实。

### 为什么不再按版本号触发（2026-10-18 修订）

旧规则是「仅在上游发布 `0.x.0`(minor) 时集中同步，不追 patch」。该规则的三个前提经实测已失效：

| 旧前提 | 实测 | 证据 |
|---|---|---|
| 上游存在 `0.x.0` 这种 minor 节奏 | ❌ 上游已进入 1.x | `v1.3.0`（2026-10-15） |
| minor 之间不引入破坏性变更，patch 可以不管 | ❌ **patch 里也会塞 Breaking** | `v1.0.3` 是 patch，却改了 provider 名（Azure `azure-openai-responses` → `azure`），用户需改 `auth.json` |
| 反过来，minor 一定带破坏性 | ❌ 不成立 | `v1.1.0` / `v1.3.0` 是 minor，Breaking 段为 0 条 |
| 「标了 Breaking 的段」是可信信号 | ❌ **不可信** | `v0.99.0` / `v1.0.0` 的 Breaking 段为空，却换了构建体系与主题体系、首次引入 MCP/codemode |
| 追 patch 不现实（1.7 天/release） | ✅ 仍成立，且更强 | 1.x 中位 1.87 天 |

**结论：版本号不能作为触发信号，改用「变更内容」触发。**

### 触发源：包级 CHANGELOG，不是 Release Notes

上游**每个包都有 `CHANGELOG.md`**，按 `## [版本] - 日期` 分节，节内含 `### Breaking Changes` 子节。

> ⚠️ **不要用 GitHub Release Notes 做依据**——它以 TUI / coding-agent 为主，
> SDK 侧变更会被大量终端渲染条目淹没。

扫描器已就位：

```bash
python3 scripts/upstream_scan.py          # 报告增量与 Breaking
python3 scripts/upstream_scan.py --json   # 机器可读
```

它做三件事：**只报基线之后的增量、按包分组、显式暴露「上游已删除但本仓仍有包」**。

---

## 同步流程 checklist

### 1. 扫描增量

```bash
python3 scripts/upstream_scan.py
```

对每个 ⚠ 命中的包，读它 `### Breaking Changes` 节下的条目原文（扫描器只给摘要，完整条目要回上游看）。

### 2. 建立映射

上游包 → 本仓包的映射**不是恒定的**，每轮都要重新确认（1.0 期间就发生过拆包/并包）：

| 上游包 | 本仓包 |
|---|---|
| `ai` | `pi-ai` |
| `agent` | `pi-agent-core` |
| `durable` | `pi-durable` |
| `mcp` / `codemode` | `pi-mcp` / `pi-codemode` |
| `coding-agent` | `pi-coding-agent` |
| `chord` | `pi-chord` |
| `server` / `protocol` / `client` | **上游已删除**（v1.2.0） |

### 3. 逐包 diff 对照（按依赖顺序，自底向上）

```
pi-ai → pi-chord → pi-durable → { pi-mcp, pi-codemode } → pi-coding-agent
```

重点检查三类高频变更文件：
- `*/src/types.ts` → 类型定义
- 核心循环（`agent-loop.ts`、`agent.ts`）
- provider 适配（`ai/src/api/*.ts`、`ai/src/providers/*.ts`）

### 4. 更新各包 `PORTING.md`

- 「有意偏离」：确认偏离项是否仍成立
- 「cherry-pick」：清理已包含在新版中的旧 patch
- 新增偏离项**必须写明原因并给上游 `文件:行号`**

### 5. 运行质量门（与 `.github/workflows/ci.yml` 完全一致）

```bash
uv sync --all-packages --all-extras
uv run ruff check packages/ .github/scripts/
uv run ruff format --check packages/ .github/scripts/
uv run mypy
uv run pytest -q
```

> ⚠️ **`uv sync` 必须带 `--all-packages --all-extras`**（与 `ci.yml` 一致）。
> 只写 `--all-extras` **不够**——它作用于**根项目**的 extras，而根项目没有 extras；
> workspace 成员的 extras 需要 `--all-packages` 才会被一并同步。
> 不带 extras 时，`pi-durable[images]` 的 Pillow 不会安装，`pi_durable.images` 会让
> `mypy` 报 `Cannot find implementation or library stub for module named "PIL"`，
> 且那条路径的测试会**静默 skip**（零覆盖）。

> 注意：是 `uv run mypy`（用 `[tool.mypy].files`），**不是** `uv run mypy packages"`——
> 后者会把各包同名测试模块一起拉进来，报 `Duplicate module named "test_integration"`。

集成测试（真实 LLM 调用，需 API key、消耗额度）：

```bash
uv run pytest -m integration
```

### 6. 更新元数据并提交

- `UPSTREAM_VERSION` 更新为新版本号
- 本文件末尾「同步历史」追加一行
- `README.md` 顶部同步状态表
- 提交：`chore(sync): 同步上游 vX.Y.Z`

---

## 完成判据

一轮同步算完成，当且仅当：

1. `ci.yml` 的 5 条命令全绿
2. 集成测试在 DeepSeek 通道上通过（本机 `.env` 已有 key）
3. 扫描器对该包不再报出未处理的 Breaking 条目（或已在 `PORTING.md` 明确登记为「有意不跟」并说明理由）

---

## 发布注意

- 打 tag 后**必须创建 GitHub Release**，否则 `.github/workflows/publish.yml` 不会触发
  （v0.85.1 就是因为只推了 tag 没建 Release，导致从未发布到 PyPI）
- 发布记录见 [Releases](https://github.com/encyc/pi-py/releases) 与 [CHANGELOG.md](./CHANGELOG.md)

---

## 同步历史

| 日期 | 上游版本 | 说明 |
|---|---|---|
| 2026-07-22 | 0.81.1 | 推翻重建，建立新基线 |
| 2026-07-27 | 0.82.1 | 适配受约束工具采样、可取消 provider 重试与摘要请求隔离 |
| 2026-07-30 | 0.83.0 | 适配 pending/raw stop reason、HTTP client 注入与 OpenAI 工具增量修复 |
| 2026-08-12 | 0.84.1 | 破例同步 patch：泛型采样参数、thinking_token_budget、无 finish_reason 流推断、Anthropic 初始块保留、shouldStopAfterTurn 等 |
| 2026-09-07 | 0.85.1 | minor 0.85 + patch：reasoning_details 回放、thinkingTokenBudgetField 泛化、strict schema 转换、toolChoice、Anthropic fallback/mid-convo effort、prepareNextTurn 时机重排、并行工具取消、bash 信号退出码。**未发布到 PyPI（仅推 tag 未建 Release）** |
| _进行中_ | _1.3.0_ | 追赶上游 1.0 大改版：包重组（`agent` 瘦身、`session-backends`→`durable`、新增 `mcp`/`codemode`/`env`、`server`/`protocol`/`client` 删除）。见 [`recon/00-catchup-plan.md`](./recon/00-catchup-plan.md) |
