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

3. **本地验证**：
   ```bash
   uv run ruff check     # lint
   uv run ruff format    # 格式化
   uv run mypy           # 类型检查（strict）
   uv run pytest         # 单元测试
   ```

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
│   ├── pi-ai/            # 统一 LLM API（OpenAI + Anthropic + retry）
│   ├── pi-agent-core/    # agent 循环引擎 + harness
│   ├── pi-storage-sqlite/ # SQLite 会话存储
│   ├── pi-coding-agent/  # 编码 agent SDK
│   └── pi-server/        # agent 服务化
├── SYNC.md               # 上游同步机制说明
├── UPSTREAM_VERSION      # 当前对齐的上游版本
├── pyproject.toml        # uv workspace 根配置
└── .github/workflows/    # CI
```

## 代码风格

- **类型**：全部函数必须带类型注解（mypy strict 模式）
- **格式**：ruff 管理（line-length=100，双引号）
- **Pydantic v2**：所有消息/内容类型用 BaseModel
- **测试**：核心逻辑必须有测试覆盖；新 feature 带测试
- **导入**：`from __future__ import annotations` 在每文件顶部

## 上游同步

本项目追踪 [earendil-works/pi](https://github.com/earendil-works/pi)。
同步策略见 [`SYNC.md`](./SYNC.md)。

如果发现上游有新的变更需要同步，请：
1. 参考 `SYNC.md` 的同步流程
2. 更新 `UPSTREAM_VERSION`
3. 更新对应包的 `PORTING.md`

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
