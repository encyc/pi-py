# 贡献指南

欢迎对 pi-py 做出贡献！本项目是 [earendil-works/pi](https://github.com/earendil-works/pi) 的 Python SDK 复刻。

## 快速开始

```bash
git clone https://github.com/earendil-works/pi-py.git
cd pi-py
uv sync          # 安装所有依赖
uv run pytest    # 确保测试通过
```

环境要求：Python 3.11+，[uv](https://docs.astral.sh/uv/)。

## 开发流程

1. **Fork 并创建分支**：从 `main` 切出 `feat/xxx` 或 `fix/xxx`

2. **编码**：遵循现有代码风格（ruff 会自动检查）

3. **本地验证**（与 `.github/workflows/ci.yml` 完全一致）：
   ```bash
   uv sync --all-packages --all-extras
   uv run ruff check packages/ .github/scripts/
   uv run ruff format --check packages/ .github/scripts/
   uv run mypy                     # 用 [tool.mypy].files，不要加 packages 参数
   uv run pytest -q                # 单元测试（默认跳过 integration）
   ```
   注意：`uv run mypy packages` 会把各包同名测试模块一起拉进来，报
   `Duplicate module named "test_integration"`——这不是类型缺陷，用 `uv run mypy` 即可。

   注意：`uv sync` **必须带 `--all-packages --all-extras`**。只写 `--all-extras` 不够
   （它只作用于根项目的 extras）；不带 extras 时 `pi-durable[images]` 的 Pillow 不会装，
   `mypy` 会报 `Cannot find implementation or library stub for module named "PIL"`，
   且那条路径的测试会静默 skip。

4. **提交**：推荐 [Conventional Commits](https://www.conventionalcommits.org/) 格式
   ```
   feat(pi-ai): 新增 Google provider
   fix(pi-agent-core): 修复 steering 队列并发问题
   ```

5. **发起 PR**：描述变更内容和原因，关联相关 issue

## 项目结构

```
pi-py/
├── packages/
│   ├── pi-ai/             # 统一 LLM API（OpenAI + Anthropic + retry）
│   ├── pi-chord/          # invocation-scoped context microkernel
│   ├── pi-durable/        # 可移植执行环境 + 会话存储
│   ├── pi-agent-core/     # agent 循环引擎
│   ├── pi-storage-sqlite/ # SQLite 会话存储
│   └── pi-coding-agent/   # 编码 agent SDK
├── scripts/               # 上游扫描器（upstream_scan.py）
├── .github/scripts/       # 发布检查脚本（tag/Release、版本、PyPI 回读）
├── SYNC.md                # 上游同步机制说明
├── UPSTREAM_VERSION       # 当前对齐的上游版本
├── pyproject.toml         # uv workspace 根配置
└── .github/workflows/     # CI / 发布 / 发布守卫
```

> 上游 `server` / `protocol` / `client` 三个包已在 v1.2.0 被删除，本仓的
> `pi-server` 因此于同期下线（见 `CHANGELOG.md`），不再发布。

## 代码风格

- **类型**：全部函数必须带类型注解（mypy strict 模式）
- **格式**：ruff 管理（line-length=100，双引号）
- **Pydantic v2**：所有消息/内容类型用 BaseModel
- **测试**：核心逻辑必须有测试覆盖；新 feature 带测试
- **导入**：`from __future__ import annotations` 在每文件顶部

## 测试准则

### 新增测试必须自证「判别力」

**「测试通过」有两种含义**：① 被测特性真的对；② 测试是恒真的（断言太松、被 mock
吞掉、时序没命中、测的其实是别的东西）。**只看「现在绿」无法区分这两者。**

**排除 ② 的唯一办法是让它红一次**：把修复临时改回去（或构造一个必然命中的输入），
确认测试会失败，再恢复确认变绿。**配套判据：如果换成错误实现它仍然绿，那它测的
不是那个特性。**

实操要点：

- 不必真回滚——改一行、跑一次、改回来即可
- **必须记录这次验证**（哪条红了、红在哪一行断言），否则别人无从复核
- 无法回退时，退而求其次：构造必然命中的输入，**并证明它确实命中了目标路径**

配套技巧：

- **受控假件把概率事件变必然**（例如让每次读钟前进 1ms，把「0.1% 的竞态」变成必现）
- **假件本身也要被验证（用例自证）**——假件失效则整个用例失去判别力
- 顺手检查既有测试是否已覆盖：若新用例变红而既有用例仍绿，**同时证明了「新用例非重复」
  与「既有用例守护能力不足」**
- 对**有真实动机的未来改动**加静态断言（例如「本端其余地方主张逐字对齐上游」时，
  一条「不要照上游换回去」的源码断言就是必要的）

这条与「**用对方做不到的输入证明能力边界**」是同一原则的两面：那条证明**支持面**，
这条证明**发现能力**；共同准绳是**可证伪**。

### 修「静默失效」类 bug 时，判据不能是返回值

静默失效的定义就是「返回值正常」。用返回值验证，等于用「症状消失」证明「病好了」。

要盯**副通道**（stderr / 日志 / 线程状态 / 副作用），并把它做成断言。典型：某处
`wait()` 在后台线程里溢出，主路径照常返回 `ok=True`——**只有 stderr 有 traceback**。

**修完一处不要收工**：同一机制常在多处实现，修完第一个后**重新观察副通道**而不是返回值。

## 上游同步

本项目追踪 [earendil-works/pi](https://github.com/earendil-works/pi)。完整规则见
[`SYNC.md`](./SYNC.md)，要点：

- **节拍**：每 2 周一次集中同步；扫描器命中「破坏性变更」时提前处理。**不再按上游
  版本号（minor/patch）触发**——上游 1.x 的 patch 里也会出现 breaking，minor 也可能
  只是 feature（依据与实测数据见 SYNC.md「为什么不再按版本号触发」）。
- **触发源是上游各包的 `CHANGELOG.md`，不是 GitHub Release Notes**：后者以
  TUI/coding-agent 为主，SDK 侧变更会被大量终端渲染条目淹没。
- **扫描增量**：
  ```bash
  python3 scripts/upstream_scan.py          # 报告基线之后的增量与 Breaking
  python3 scripts/upstream_scan.py --json   # 机器可读
  ```
- **映射会变**：上游包 → 本仓包的对应关系每轮都要重新确认（1.0 期间发生过拆包/并包；
  `server`/`protocol`/`client` 已在上游 v1.2.0 删除）。
- **收尾**：更新 `UPSTREAM_VERSION`、`SYNC.md` 同步历史、`README.md` 顶部状态表、
  对应包的 `PORTING.md`（新增偏离必须写明原因与上游 `文件:行号`）。

一轮同步算完成的条件（见 SYNC.md「完成判据」）：CI 的 5 条命令全绿、集成测试在
DeepSeek 通道通过、扫描器不再报未处理的 Breaking（或已登记为「有意不跟」并说明理由）。

## 发布

打 tag 与建 GitHub Release **两件事都要做**：

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes-file /tmp/notes.md
```

`publish.yml` 由 `release: published` 触发；只推 tag 不建 Release 不会有任何发布
（v0.85.1 就是这样漏发的）。发布链路自带三道检查：

- `release-guard.yml`：每天 + push `main` 时检查「有 tag 但无 Release」，命中即失败；
- `publish.yml` 的 `verify-release`：校验 tag 存在、`packages/*` 版本 lockstep 与 tag
  一致、`CHANGELOG.md` 有对应小节；
- `publish.yml` 发布后回读 PyPI，要求每个包 latest 等于本次 tag（防止
  `skip-existing` 掩盖「其实没传上去」）。

补发历史版本：`Actions → Publish to PyPI → Run workflow`，填入已存在的 `tag`。

### ⚠️ 两种触发方式用的**不是同一份** `publish.yml`

这是实测确认的（否则会得出错误的心智模型）：

| 触发方式 | workflow 文件取自 |
|---|---|
| `release: published` | **该 tag 自己的内容** |
| `workflow_dispatch` | **默认分支**（所选 ref）的内容 |

**后果**：给**历史 tag** 补发时，`release` 事件跑的是**那个 tag 当时**的 `publish.yml` ——
若该 tag 早于某项硬化（例如 `verify-release` 与 PyPI 回读是后加的），**那些检查不会生效**。

**⇒ 补发历史版本请一律走 `workflow_dispatch` 并填 tag**，它才会用到当前的校验链路。

配套地，工作流里**校验脚本取自默认分支、被校验内容取自 tag**（两次检出）：
历史 tag 上通常没有 `.github/scripts/`，把两者放在同一次检出里会让补发失败在
「脚本不存在」这一步。

### ⚠️ PyPI 的 JSON 端点会被 CDN 缓存

`https://pypi.org/pypi/<pkg>/json` 可能返回**缓存**的旧版本号，于是**明明发上去了却回读成旧版本**。

**判据**：回读要**两个证据**取其一或并用 ——
① 版本专用端点 `https://pypi.org/pypi/<pkg>/<version>/json`（存在即已上传）；
② 主端点带 `Cache-Control: no-cache`。

**不要**用一次主端点的回读就判定「没发上去」—— 那会让你去排查一个不存在的故障。

## 添加新 provider

1. 在 `packages/pi-ai/src/pi_ai/providers/` 创建新文件
2. 实现流式适配（参考 `openai_provider.py` 的模式）
3. 在 `packages/pi-ai/src/pi_ai/models.py` 的 `register_builtins()` 注册
4. 添加纯逻辑测试 + 集成测试（如果有 API key）
5. 更新 PORTING.md

## 获取帮助

- 提交 [Issue](https://github.com/earendil-works/pi-py/issues)
- 参考上游 [Pi 文档](https://pi.dev)
- 参考 [agentskills.io](https://agentskills.io) 了解技能规范
