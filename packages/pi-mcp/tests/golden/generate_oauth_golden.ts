/**
 * 生成 `oauth_pure_functions.json`：**直接执行上游 TypeScript 源码**，把纯函数的输出
 * 逐字节固化成黄金样本，供 `tests/test_oauth_golden.py` 回放。
 *
 * 为什么这么做：`parseWwwAuthenticate` / `selectResource` / `parse*` 这些函数的行为细节
 * （空值算缺席、URL 规范化、错误文案、未知键保留）靠读代码容易漏；用 Node 的类型剥离
 * 直接跑上游源码，可以做到字节级对齐（见 `PORTING.md` 的「黄金样本」一节）。
 *
 * 复现：
 *   UPSTREAM=/path/to/pi-up/v1.3.0/packages/mcp \
 *     node --experimental-strip-types tests/golden/generate_oauth_golden.ts \
 *     > tests/golden/oauth_pure_functions.json
 *
 * 需要 Node ≥ 22.6（`--experimental-strip-types`）。**不需要** `npm install`：这些模块
 * 只 import 彼此，没有第三方依赖。
 */
const UPSTREAM = process.env.UPSTREAM ?? "/tmp/pi-up/v1.3.0/packages/mcp";
const src = (path: string) => `file://${UPSTREAM}/src/${path}`;

const { buildAuthorizationServerDiscoveryUrls, parseWwwAuthenticate, resourceUrlFromServerUrl, selectResource } =
  await import(src("oauth/discovery.ts"));
const { stepUpScope } = await import(src("oauth/flow.ts"));
const { parseAuthorizationServerMetadata, parseClientInformation, parseOAuthTokens, parseProtectedResourceMetadata } =
  await import(src("oauth/types.ts"));

const out: unknown[] = [];
const run = (name: string, fn: () => unknown) => {
  try {
    // `undefined ?? null`：JSON.stringify 会把值为 undefined 的键整个丢掉，
    // 那样「返回 undefined」这条规格就静默消失了（selectResource 就有三例）。
    out.push([name, fn() ?? null]);
  } catch (error) {
    out.push([name, { __error: error instanceof Error ? error.message : String(error) }]);
  }
};

for (const header of [
  null, "", "Basic realm=x", 'Bearer scope="repo admin", error="x"', 'Bearer scope=""',
  "DPoP scope=repo", 'Bearer resource_metadata="https://a.example/meta"',
  'Bearer resource_metadata=":::"', 'bearer error="insufficient_scope", scope="a b", error_description="why"',
  "Bearer resource_metadata=https://plain.example/meta", 'Bearer scope="a,b"', 'Digest realm="x"',
  'Bearer error=""', "Bearer", 'Bearer  scope="x"',
]) {
  run(`parseWwwAuthenticate:${JSON.stringify(header)}`, () =>
    JSON.parse(JSON.stringify(parseWwwAuthenticate(header))));
}

for (const url of [
  "https://a.example", "https://a.example/", "https://a.example/tenant",
  "https://a.example/tenant/", "https://a.example/a/b",
]) {
  run(`buildDiscoveryUrls:${url}`, () =>
    buildAuthorizationServerDiscoveryUrls(url).map((entry) => [entry.url.href, entry.type]));
}

for (const url of [
  "https://a.example/mcp", "https://a.example/mcp#frag", "https://a.example/mcp?x=1#f",
  "https://a.example/", "https://a.example:8443/mcp", "https://a.example", "https://a.example?x=1",
]) {
  run(`resourceUrlFromServerUrl:${url}`, () => resourceUrlFromServerUrl(url).href);
}

for (const server of ["https://a.example", "https://a.example/mcp", "https://b.example/mcp"]) {
  for (const resource of [undefined, "https://a.example/mcp", "https://a.example", "https://a.example/mcp/deep", "https://b.example/mcp", "https://a.example/other"]) {
    run(`selectResource:${server}|${resource}`, () =>
      selectResource(server, resource === undefined ? undefined : { resource }));
  }
}

for (const pair of [
  ["repo read:org", "repo admin"], [undefined, "a"], ["a", ""], [undefined, undefined],
  ["a a b", "b c"], ["", "x"], ["a", undefined],
] as [string | undefined, string | undefined][]) {
  run(`stepUpScope:${JSON.stringify(pair)}`, () => stepUpScope(pair[0], pair[1]) ?? null);
}

const parseCases: [string, unknown, (value: unknown) => unknown][] = [
  ["prm:minimal", { resource: "https://a.example/mcp" }, parseProtectedResourceMetadata],
  ["prm:servers+scopes", { resource: "https://a.example/mcp", authorization_servers: ["https://a.example", "https://b.example"], scopes_supported: ["a", "b"], extra: 1 }, parseProtectedResourceMetadata],
  ["prm:bad-server", { resource: "https://a.example/mcp", authorization_servers: ["not a url"] }, parseProtectedResourceMetadata],
  ["prm:no-resource", { authorization_servers: [] }, parseProtectedResourceMetadata],
  ["prm:javascript", { resource: "javascript:alert(1)" }, parseProtectedResourceMetadata],
  ["prm:null-scopes", { resource: "https://a.example/mcp", scopes_supported: null }, parseProtectedResourceMetadata],
  ["asm:minimal", { issuer: "https://a.example", authorization_endpoint: "https://a.example/authorize", token_endpoint: "https://a.example/token", response_types_supported: ["code"] }, parseAuthorizationServerMetadata],
  ["asm:full", { issuer: "https://a.example/", authorization_endpoint: "https://a.example/authorize", token_endpoint: "https://a.example/token", registration_endpoint: "https://a.example/register", scopes_supported: ["a"], response_types_supported: ["code"], grant_types_supported: ["authorization_code"], token_endpoint_auth_methods_supported: ["none"], code_challenge_methods_supported: ["S256"], client_id_metadata_document_supported: true, authorization_response_iss_parameter_supported: false, extra: { deep: true } }, parseAuthorizationServerMetadata],
  ["asm:no-response-types", { issuer: "https://a.example", authorization_endpoint: "https://a.example/authorize", token_endpoint: "https://a.example/token" }, parseAuthorizationServerMetadata],
  ["asm:empty-registration", { issuer: "https://a.example", authorization_endpoint: "https://a.example/authorize", token_endpoint: "https://a.example/token", registration_endpoint: "", response_types_supported: ["code"] }, parseAuthorizationServerMetadata],
  ["asm:bad-issuer", { issuer: "https://", authorization_endpoint: "https://a.example/authorize", token_endpoint: "https://a.example/token", response_types_supported: ["code"] }, parseAuthorizationServerMetadata],
  ["tokens:minimal", { access_token: "a", token_type: "Bearer" }, parseOAuthTokens],
  ["tokens:null-expires", { access_token: "a", token_type: "Bearer", expires_in: null }, parseOAuthTokens],
  ["tokens:string-expires", { access_token: "a", token_type: "Bearer", expires_in: "60" }, parseOAuthTokens],
  ["tokens:nan-expires", { access_token: "a", token_type: "Bearer", expires_in: "abc" }, parseOAuthTokens],
  ["tokens:empty-scope", { access_token: "a", token_type: "Bearer", scope: "" }, parseOAuthTokens],
  ["tokens:missing-type", { access_token: "a" }, parseOAuthTokens],
  ["client:minimal", { client_id: "c" }, parseClientInformation],
  ["client:extra", { client_id: "c", client_secret: "", redirect_uris: ["https://a/cb"], client_name: "n", custom: 7 }, parseClientInformation],
  ["client:issued-at", { client_id: "c", client_id_issued_at: 5, client_secret_expires_at: 9.5 }, parseClientInformation],
];
for (const [name, value, fn] of parseCases) {
  run(name, () => JSON.parse(JSON.stringify(fn(value))));
}

console.log(JSON.stringify(Object.fromEntries(out as [string, unknown][]), null, 1));
