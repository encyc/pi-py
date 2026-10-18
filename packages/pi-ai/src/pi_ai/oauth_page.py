"""OAuth 回调页面 HTML 生成。

对应上游 ``packages/ai/src/utils/oauth-page.ts``（v1.3.0，109 行）。

**这是纯渲染工具，不属于被排除的「OAuth 交互登录面」**：模块内没有登录流程、
不发网络请求、不接触任何凭据。它只把一段文案渲染成一张本地回调页面，
供 MCP OAuth（``coding-agent/src/extensions/mcp/oauth.ts``）等调用方在浏览器里
告知用户授权结果。上游把它放在 ``utils/`` 而非 ``auth/oauth/`` 也是同一取向
（v1.3.0 由 ``auth/oauth/oauth-page.ts`` 迁至 ``utils/oauth-page.ts``，
见上游 CHANGELOG 0.99.0 Changed）。

导出面与上游**严格一致**：仅 :func:`oauth_success_html` 与
:func:`oauth_error_html`；``escapeHtml`` / ``renderPage`` 在上游也未导出，
故此处保持私有。

安全边界
--------
上游**已实现** HTML 转义（``oauth-page.ts:3-10``）且顺序正确（先 ``&`` 再其余，
避免二次转义），并在插值前对四个字段**全部**转义（``:13-16``）。四个插值点
（``<title>``/``<h1>``/``<p>``/``.details``）均位于**元素内容**位置——不涉及
属性、``<style>``、``<script>`` 或 URL 上下文，因此该转义集是充分的。
本端逐条对齐，并用真实上游实现生成的参考样本做了逐字节比对。
"""

from __future__ import annotations

import re

__all__ = ["oauth_error_html", "oauth_success_html"]

#: Pi 的彩色 logo（上游 ``LOGO_SVG``，``oauth-page.ts:1``）。常量，非用户输入。
_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" aria-hidden="true">'
    '<path fill="#F09082" d="M165.29 165.29H517.36V400H400V282.65H165.29Z"/>'
    '<path fill="#4D9ABF" d="M165.29 282.65H282.65V400H400V517.36H282.65V634.72H165.29Z"/>'
    '<path fill="#F1BE58" d="M517.36 400H634.72V634.72H517.36Z"/>'
    "</svg>"
)

#: 页面模板。占位符为 ``__NAME__`` 形式，由 :func:`_render_page` **一次**替换。
_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <style>
    :root {
      --text: #fafafa;
      --text-dim: #a1a1aa;
      --page-bg: #09090b;
      --font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji";
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }
    * { box-sizing: border-box; }
    html { color-scheme: dark; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: var(--page-bg);
      color: var(--text);
      font-family: var(--font-sans);
      text-align: center;
    }
    main {
      width: 100%;
      max-width: 560px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .logo {
      width: 72px;
      height: 72px;
      display: block;
      margin-bottom: 24px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 28px;
      line-height: 1.15;
      font-weight: 650;
      color: var(--text);
    }
    p {
      margin: 0;
      line-height: 1.7;
      color: var(--text-dim);
      font-size: 15px;
    }
    .details {
      margin-top: 16px;
      font-family: var(--font-mono);
      font-size: 13px;
      color: var(--text-dim);
      white-space: pre-wrap;
      word-break: break-word;
    }
  </style>
</head>
<body>
  <main>
    <div class="logo">__LOGO__</div>
    <h1>__HEADING__</h1>
    <p>__MESSAGE__</p>
    __DETAILS__
  </main>
</body>
</html>"""

_TOKEN_RE = re.compile(r"__([A-Z]+)__")


def _escape_html(value: str) -> str:
    """HTML 文本转义。逐字对应上游 ``escapeHtml``（``oauth-page.ts:3-10``）。

    顺序关键：**先**替换 ``&``，否则后续步骤产生的实体会被再次转义
    （``<`` -> ``&lt;`` -> ``&amp;lt;``，页面显示成 ``&lt;`` 而非 ``<``）。
    上游顺序正确，本端保持。

    转义集为 ``& < > " '``；缺少的 ``=``/反引号在本模块的插值上下文中
    （元素内容，非无引号属性/JS/CSS）不构成注入面。
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_page(
    *,
    title: str,
    heading: str,
    message: str,
    details: str | None = None,
) -> str:
    """渲染整页。对应上游 ``renderPage``（``oauth-page.ts:12-92``）。

    四个插值字段**全部**先转义再进模板。``details`` 为空串或 ``None`` 时整块
    ``<div class="details">`` 不输出（上游 :16 与 :88 的双重真值判断等价于
    Python 的 ``if safe_details:``）。

    占位符采用**单次** ``re.sub`` 替换：若逐次 ``str.replace``，用户文案里若
    恰好含 ``__MESSAGE__`` 之类的标记，会在后续轮次被**再次替换**，形成内容
    注入（用户可控内容被当成模板指令）。一次扫描即杜绝该类问题。
    """
    safe_details = _escape_html(details) if details else ""
    values = {
        "LOGO": _LOGO_SVG,
        "TITLE": _escape_html(title),
        "HEADING": _escape_html(heading),
        "MESSAGE": _escape_html(message),
        "DETAILS": f'<div class="details">{safe_details}</div>' if safe_details else "",
    }
    return _TOKEN_RE.sub(lambda match: values[match.group(1)], _PAGE_TEMPLATE)


def oauth_success_html(message: str) -> str:
    """授权成功页面。对应上游 ``oauthSuccessHtml``（``oauth-page.ts:94-100``）。

    :param message: 页面正文文案（会被 HTML 转义）
    """
    return _render_page(
        title="Authentication successful",
        heading="Authentication successful",
        message=message,
    )


def oauth_error_html(message: str, details: str | None = None) -> str:
    """授权失败页面。对应上游 ``oauthErrorHtml``（``oauth-page.ts:102-108``）。

    :param message: 页面正文文案（会被 HTML 转义）
    :param details: 可选的等宽字体细节块，例如 ``error=access_denied&state=...``；
        为空串或 ``None`` 时不渲染该块（会被 HTML 转义）
    """
    return _render_page(
        title="Authentication failed",
        heading="Authentication failed",
        message=message,
        details=details,
    )
