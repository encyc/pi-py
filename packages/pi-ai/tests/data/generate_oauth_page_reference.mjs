// 生成 oauth-page 的"上游黄金样本"，供 test_oauth_page.py 做逐字节比对。
//
// 为什么需要：本端 OAuth 页面必须与上游渲染结果一致（下游 MCP OAuth 用它显示
// 本地回调结果）。靠人读代码判断"是否一致"不可靠——此脚本**直接执行上游
// TypeScript 源文件**产出权威输出。
//
// 用法（需 Node >= 22.6，原生类型剥离）：
//   node generate_oauth_page_reference.mjs <上游 oauth-page.ts 路径> > oauth-page-reference.json
// 例：
//   node generate_oauth_page_reference.mjs \
//     /tmp/pi-up/v1.3.0/packages/ai/src/utils/oauth-page.ts > oauth-page-reference.json
//
// 注意：上游文件是只读参考，勿修改。

import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const sourcePath = process.argv[2];
if (!sourcePath) {
	console.error("usage: node generate_oauth_page_reference.mjs <upstream oauth-page.ts>");
	process.exit(2);
}

const { oauthSuccessHtml, oauthErrorHtml } = await import(pathToFileURL(sourcePath).href);

const cases = {
	"success-simple": oauthSuccessHtml("You can close this window."),
	"success-html-injection": oauthSuccessHtml('<script>alert("xss")</script>'),
	"success-amp": oauthSuccessHtml("Tom & Jerry <b>bold</b>"),
	"success-quotes": oauthSuccessHtml(`He said "hi" & it's 'fine'`),
	"error-message-only": oauthErrorHtml("Something went wrong"),
	"error-with-details": oauthErrorHtml("Sign-in failed", "error=access_denied&state=abc"),
	"error-empty-details": oauthErrorHtml("Sign-in failed", ""),
	"error-injection-both": oauthErrorHtml(
		'<img src=x onerror=alert(1)>',
		"<script>document.cookie</script>",
	),
	"error-entity-double": oauthErrorHtml("&amp;", "&lt;script&gt;"),
	"error-unicode": oauthErrorHtml("失败：授权被拒绝 🔒", "状态=已取消"),
	"error-newlines": oauthErrorHtml("line1\nline2", "a\nb"),
};

const payload = {
	_provenance: {
		source: "packages/ai/src/utils/oauth-page.ts",
		upstreamRef: "earendil-works/pi@v1.3.0",
		generatedBy: "tests/data/generate_oauth_page_reference.mjs",
		note: "由上游 TypeScript 实现原样产出；test_oauth_page.py 逐字节比对。请勿手工编辑。",
	},
	cases,
};

writeFileSync(new URL("./oauth-page-reference.json", import.meta.url), JSON.stringify(payload, null, "\t") + "\n");
console.log("wrote oauth-page-reference.json");
