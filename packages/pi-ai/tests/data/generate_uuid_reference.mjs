// 生成 uuidv7 的"上游黄金样本"，供 test_uuid.py 做**逐字节**比对。
//
// 为什么需要：uuidv7 的位布局（尤其序列在字节 6–11 上的跨字节打包、以及字节 11
// 最低位保留"首次随机"的 bit0）靠人读代码极易出错。此脚本用**确定性随机流**驱动
// 上游真实实现，产出可复现的权威输出。
//
// 用法（需 Node >= 22.6，原生类型剥离）：
//   node generate_uuid_reference.mjs <上游 utils/uuid.ts 路径> > uuid-reference.json
// 例：
//   node generate_uuid_reference.mjs \
//     /tmp/pi-up/v1.3.0/packages/ai/src/utils/uuid.ts > uuid-reference.json
//
// 确定性字节流公式（必须与 test_uuid.py 中完全一致，否则比对无意义）：
//   第 c 次调用（c 从 0 起）的第 i 字节 = (c * 31 + i * 7 + 11) & 0xff
//
// 注意：上游文件只读参考，勿修改。

import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const sourcePath = process.argv[2];
if (!sourcePath) {
	console.error("usage: node generate_uuid_reference.mjs <upstream utils/uuid.ts>");
	process.exit(2);
}

const { uuidv7 } = await import(pathToFileURL(sourcePath).href);

let counter = 0;
Object.defineProperty(globalThis, "crypto", {
	value: {
		getRandomValues: (arr) => {
			const c = counter++;
			for (let i = 0; i < arr.length; i++) arr[i] = (c * 31 + i * 7 + 11) & 0xff;
			return arr;
		},
	},
	configurable: true,
	writable: true,
});

const cases = [];
for (const ts of [0, 1, 255, 256, 0xffffffffffff, 1700000000000, 1700000000001]) {
	cases.push({ ts, id: uuidv7(ts) });
}
// 同毫秒连发 5 个：验证序列递增
for (let i = 0; i < 5; i++) cases.push({ ts: 1700000000000, id: uuidv7(1700000000000) });

const payload = {
	_provenance: {
		source: "packages/ai/src/utils/uuid.ts",
		upstreamRef: "earendil-works/pi@v1.3.0",
		generatedBy: "tests/data/generate_uuid_reference.mjs",
		deterministicRandom: "byte(c, i) = (c * 31 + i * 7 + 11) & 0xff",
		note: "由上游 TypeScript 实现原样产出；test_uuid.py 以相同确定性随机流重放并逐字节比对。请勿手工编辑。",
	},
	cases,
};

writeFileSync(new URL("./uuid-reference.json", import.meta.url), JSON.stringify(payload, null, "\t") + "\n");
console.log("wrote uuid-reference.json");
