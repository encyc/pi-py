"""OAuth 纯函数的黄金样本回归。

`tests/golden/oauth_pure_functions.json` 是**直接执行上游 TypeScript 源码**得到的输出
（生成脚本见 `tests/golden/generate_oauth_golden.ts`），本用例把它逐条回放到
Python 实现上，做**字节级**比对。

覆盖：``parse_www_authenticate`` / ``build_authorization_server_discovery_urls`` /
``resource_url_from_server_url`` / ``select_resource`` / ``step_up_scope`` /
``parse_protected_resource_metadata`` / ``parse_authorization_server_metadata`` /
``parse_oauth_tokens`` / ``parse_client_information``。

为什么值钱：这些函数的行为细节（``null``/``""`` 算缺席、URL 规范化、错误文案、
未知键保留）靠读代码容易漏。样本是上游的**实际输出**，不是我的理解。

刷新样本（需要 Node ≥ 22.6，不需要 npm install）：

```bash
UPSTREAM=/tmp/pi-up/v1.3.0/packages/mcp \\
  node --experimental-strip-types tests/golden/generate_oauth_golden.ts \\
  > tests/golden/oauth_pure_functions.json
```
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pi_mcp.oauth import (
    build_authorization_server_discovery_urls,
    parse_www_authenticate,
    resource_url_from_server_url,
    select_resource,
    step_up_scope,
)
from pi_mcp.oauth.types import (
    parse_authorization_server_metadata,
    parse_client_information,
    parse_oauth_tokens,
    parse_protected_resource_metadata,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "oauth_pure_functions.json"


def _canonical(value: Any) -> Any:
    """走一遍 JSON，让 Python 值与 ``JSON.stringify`` 的语义对齐。"""
    return json.loads(json.dumps(value, default=str, separators=(",", ":")))


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return _canonical(fn())
    except Exception as error:  # noqa: BLE001 - 上游的抛错也是规格的一部分
        return {"__error": str(error)}


def _replay(name: str) -> Any:
    """把一条黄金样本的名字还原成 Python 调用。"""
    kind, _, argument = name.partition(":")
    if kind == "parseWwwAuthenticate":
        return _call(lambda: parse_www_authenticate(json.loads(argument)))
    if kind == "buildDiscoveryUrls":
        return _call(
            lambda: [
                [href, sort] for href, sort in build_authorization_server_discovery_urls(argument)
            ]
        )
    if kind == "resourceUrlFromServerUrl":
        return _call(lambda: resource_url_from_server_url(argument))
    if kind == "selectResource":
        server, _, resource = argument.partition("|")
        metadata = None if resource == "undefined" else {"resource": resource}
        return _call(lambda: select_resource(server, metadata))
    if kind == "stepUpScope":
        granted, challenged = json.loads(argument)
        return _call(lambda: step_up_scope(granted, challenged))
    raise AssertionError(f"未知的黄金样本: {name}")


#: 解析函数的样本名 → 输入构造（名字里带 ``:`` 的是解析类）。
_PARSE_INPUTS: dict[str, tuple[dict[str, Any], Callable[[Any], Any]]] = {
    "prm:minimal": ({"resource": "https://a.example/mcp"}, parse_protected_resource_metadata),
    "prm:servers+scopes": (
        {
            "resource": "https://a.example/mcp",
            "authorization_servers": ["https://a.example", "https://b.example"],
            "scopes_supported": ["a", "b"],
            "extra": 1,
        },
        parse_protected_resource_metadata,
    ),
    "prm:bad-server": (
        {"resource": "https://a.example/mcp", "authorization_servers": ["not a url"]},
        parse_protected_resource_metadata,
    ),
    "prm:no-resource": ({"authorization_servers": []}, parse_protected_resource_metadata),
    "prm:javascript": ({"resource": "javascript:alert(1)"}, parse_protected_resource_metadata),
    "prm:null-scopes": (
        {"resource": "https://a.example/mcp", "scopes_supported": None},
        parse_protected_resource_metadata,
    ),
    "asm:minimal": (
        {
            "issuer": "https://a.example",
            "authorization_endpoint": "https://a.example/authorize",
            "token_endpoint": "https://a.example/token",
            "response_types_supported": ["code"],
        },
        parse_authorization_server_metadata,
    ),
    "asm:full": (
        {
            "issuer": "https://a.example/",
            "authorization_endpoint": "https://a.example/authorize",
            "token_endpoint": "https://a.example/token",
            "registration_endpoint": "https://a.example/register",
            "scopes_supported": ["a"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": False,
            "extra": {"deep": True},
        },
        parse_authorization_server_metadata,
    ),
    "asm:no-response-types": (
        {
            "issuer": "https://a.example",
            "authorization_endpoint": "https://a.example/authorize",
            "token_endpoint": "https://a.example/token",
        },
        parse_authorization_server_metadata,
    ),
    "asm:empty-registration": (
        {
            "issuer": "https://a.example",
            "authorization_endpoint": "https://a.example/authorize",
            "token_endpoint": "https://a.example/token",
            "registration_endpoint": "",
            "response_types_supported": ["code"],
        },
        parse_authorization_server_metadata,
    ),
    "asm:bad-issuer": (
        {
            "issuer": "https://",
            "authorization_endpoint": "https://a.example/authorize",
            "token_endpoint": "https://a.example/token",
            "response_types_supported": ["code"],
        },
        parse_authorization_server_metadata,
    ),
    "tokens:minimal": ({"access_token": "a", "token_type": "Bearer"}, parse_oauth_tokens),
    "tokens:null-expires": (
        {"access_token": "a", "token_type": "Bearer", "expires_in": None},
        parse_oauth_tokens,
    ),
    "tokens:string-expires": (
        {"access_token": "a", "token_type": "Bearer", "expires_in": "60"},
        parse_oauth_tokens,
    ),
    "tokens:nan-expires": (
        {"access_token": "a", "token_type": "Bearer", "expires_in": "abc"},
        parse_oauth_tokens,
    ),
    "tokens:empty-scope": (
        {"access_token": "a", "token_type": "Bearer", "scope": ""},
        parse_oauth_tokens,
    ),
    "tokens:missing-type": ({"access_token": "a"}, parse_oauth_tokens),
    "client:minimal": ({"client_id": "c"}, parse_client_information),
    "client:extra": (
        {
            "client_id": "c",
            "client_secret": "",
            "redirect_uris": ["https://a/cb"],
            "client_name": "n",
            "custom": 7,
        },
        parse_client_information,
    ),
    "client:issued-at": (
        {"client_id": "c", "client_id_issued_at": 5, "client_secret_expires_at": 9.5},
        parse_client_information,
    ),
}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_samples_are_present() -> None:
    """样本文件必须在库里（否则这条回归就静默失效了）。"""
    golden = _golden()
    assert len(golden) >= 70, f"黄金样本太少（{len(golden)}），可能被截断"


def test_pure_functions_match_upstream_byte_for_byte() -> None:
    """逐条回放：上游 TypeScript 的输出 == 本端 Python 的输出。"""
    golden = _golden()
    mismatches: list[str] = []
    for name, expected in golden.items():
        if name in _PARSE_INPUTS:
            value, parser = _PARSE_INPUTS[name]
            actual = _call(lambda v=value, f=parser: f(v))
        else:
            actual = _replay(name)
        if _canonical(expected) != actual:
            mismatches.append(
                f"{name}\n  上游: {json.dumps(expected, ensure_ascii=False)}\n  本端: {json.dumps(actual, ensure_ascii=False)}"
            )
    assert not mismatches, "与上游输出不一致:\n" + "\n".join(mismatches)


def test_golden_samples_cover_every_parser() -> None:
    """四个解析函数都必须在样本里出现，避免以后漏掉某个。"""
    names = set(_golden())
    for prefix in ("prm:", "asm:", "tokens:", "client:"):
        assert any(name.startswith(prefix) for name in names), f"缺少 {prefix} 的样本"
    for prefix in (
        "parseWwwAuthenticate:",
        "buildDiscoveryUrls:",
        "resourceUrlFromServerUrl:",
        "selectResource:",
        "stepUpScope:",
    ):
        assert any(name.startswith(prefix) for name in names), f"缺少 {prefix} 的样本"
