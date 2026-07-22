# 安全策略

## 报告漏洞

如果你发现安全漏洞，**请不要在公开的 Issue 中报告**。

请通过以下方式私下报告：
- Email: 维护者邮箱（见 GitHub profile）
- 或提交 [GitHub Security Advisory](https://github.com/earendil-works/pi-py/security/advisories/new)

我们会在 48 小时内确认收到，并在 7 天内提供初步评估。

## 支持版本

当前仅支持 `main` 分支的最新版本。建议及时更新以获取安全修复。

## 依赖安全

本项目依赖的第三方 Python 包（openai、anthropic、pydantic 等）的安全漏洞由其各自维护者负责。我们通过 renovate/dependabot 或手动审查来保持依赖更新。

## 模型安全

pi-py 是一个 SDK 库，不直接与 LLM API 通信以外的数据交互。安全问题通常涉及：
- **API key 泄露**：不要在代码中硬编码 API key。使用环境变量或 `StreamOptions(api_key=...)`。
- **命令注入**：`bash` 工具会执行任意 shell 命令。仅在受信任环境使用。
- **文件访问**：`read`/`edit`/`write` 工具可访问文件系统。确保 `cwd` 限定在安全目录。
