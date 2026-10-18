/**
 * 生成 jsdiff golden 语料，供 tests/test_edit_diff_parity.py 做真差分对拍。
 *
 * 用法（需要 node + npm）：
 *   mkdir -p /tmp/p && cd /tmp/p && npm install diff@8.0.4
 *   node <repo>/packages/pi-durable/tests/parity/generate-golden.js > golden.json
 *
 * 「大语料」的输入由 deterministic 的配方生成（见 bigCases()），Python 侧用同一配方重建，
 * 因此 golden 里只存**输出的 sha256**，仓库不用背几百 KB 的字符串。
 */
const crypto = require("crypto");
const Diff = require("diff");

const smallCases = () => [
  ["identical", "a\nb\nc\n", "a\nb\nc\n"],
  ["insert-middle", "a\nc\n", "a\nb\nc\n"],
  ["delete-middle", "a\nb\nc\n", "a\nc\n"],
  ["replace-one", "a\nb\nc\n", "a\nB\nc\n"],
  ["replace-multi", "a\nb\nc\nd\n", "a\nX\nY\nd\n"],
  ["two-changes-far", "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\n", "a\nB\nc\nd\ne\nf\ng\nh\ni\nj\nK\nl\n"],
  ["two-changes-adjacent", "a\nb\nc\n", "A\nB\nc\n"],
  ["change-at-start", "a\nb\nc\n", "A\nb\nc\n"],
  ["change-at-end", "a\nb\nc\n", "a\nb\nC\n"],
  ["empty-old", "", "a\nb\n"],
  ["empty-new", "a\nb\n", ""],
  ["both-empty", "", ""],
  ["no-trailing-newline", "a\nb", "a\nB"],
  ["trailing-added", "a\nb", "a\nb\n"],
  ["trailing-removed", "a\nb\n", "a\nb"],
  ["crlf", "a\r\nb\r\n", "a\r\nB\r\n"],
  ["unicode", "中文\n行二\n", "中文\n第二行\n"],
  ["emoji", "a\n🙂\n", "a\n🎉\n"],
  ["context-exactly-4", "1\n2\n3\n4\nX\n5\n6\n7\n8\n", "1\n2\n3\n4\nY\n5\n6\n7\n8\n"],
  ["context-5-each-side", "1\n2\n3\n4\n5\nX\n6\n7\n8\n9\n10\n", "1\n2\n3\n4\n5\nY\n6\n7\n8\n9\n10\n"],
  ["context-10-each-side", "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\nX\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n",
                          "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\nY\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n"],
  ["blank-lines", "a\n\n\nb\n", "a\n\nb\n"],
  ["whole-file-replace", "a\nb\n", "x\ny\n"],
  ["many-lines", Array.from({length: 30}, (_, i) => `line${i}`).join("\n") + "\n",
                  Array.from({length: 30}, (_, i) => (i === 15 ? "CHANGED" : `line${i}`)).join("\n") + "\n"],
  ["dup-lines", "a\na\na\n", "a\na\n"],
  ["interleaved", "a\nb\nc\nd\ne\n", "b\na\nd\nc\ne\n"],
  ["bare-cr", "a\rb\r", "a\rB\r"],
];

// 大语料配方 —— Python 侧 tests/test_edit_diff_parity.py::big_cases() 必须逐字镜像。
function bigCases() {
  const out = [];
  const numbered = (n) => Array.from({length: n}, (_, i) => `line ${i} some repeated words here`);
  for (const n of [100, 201, 250, 500, 1200]) {
    const a = numbered(n); const b = a.slice();
    b[Math.floor(n / 2)] = "CHANGED LINE";
    out.push([`big-${n}-one-change`, a.join("\n") + "\n", b.join("\n") + "\n"]);
  }
  {
    const n = 300; const a = numbered(n); const b = a.slice();
    b[0] = "HEAD CHANGED"; b[n - 1] = "TAIL CHANGED";
    out.push(["big-300-ends", a.join("\n") + "\n", b.join("\n") + "\n"]);
  }
  {
    const a = Array.from({length: 400}, (_, i) => (i % 2 === 0 ? "same" : `uniq${i}`)).join("\n") + "\n";
    out.push(["big-repeat-heavy", a, a.replace("uniq201", "UNIQ201")]);
  }
  {
    const lines = Array.from({length: 250}, (_, i) => `l${i}`);
    const a = lines.join("\n") + "\n";
    const mid = Array.from({length: 50}, (_, i) => `NEW${i}`).join("\n");
    out.push(["big-bulk-insert", a, lines.slice(0, 120).join("\n") + "\n" + mid + "\n" + lines.slice(120).join("\n")]);
  }
  {
    const lines = Array.from({length: 220}, (_, i) => `m${i}`);
    const a = lines.join("\n") + "\n";
    const moved = lines.slice(); const [one] = moved.splice(10, 1); moved.splice(200, 0, one);
    out.push(["big-moved-line", a, moved.join("\n") + "\n"]);
  }
  return out;
}

const sha = (s) => crypto.createHash("sha256").update(s, "utf8").digest("hex");
const encode = (oldC, newC) => ({
  diffLines: JSON.stringify(Diff.diffLines(oldC, newC).map(p => ({
    value: p.value, added: !!p.added, removed: !!p.removed, count: p.count,
  }))),
  patch: Diff.createTwoFilesPatch("f.txt", "f.txt", oldC, newC, undefined, undefined,
    { context: 4, headerOptions: Diff.FILE_HEADERS_ONLY }),
});

const golden = {
  jsdiffVersion: "8.0.4",
  note: "由 generate-golden.js 用真 diff@8.0.4 生成；小语料存全量，大语料只存 sha256（输入由配方重建）",
  small: smallCases().map(([name, oldC, newC]) => {
    const e = encode(oldC, newC);
    return { name, old: oldC, new: newC, diffLines: JSON.parse(e.diffLines), patch: e.patch };
  }),
  big: bigCases().map(([name, oldC, newC]) => {
    const e = encode(oldC, newC);
    return { name, diffLinesSha256: sha(e.diffLines), patchSha256: sha(e.patch) };
  }),
};
console.log(JSON.stringify(golden, null, 1));
