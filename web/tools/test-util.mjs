// npm run test:util
import assert from "node:assert/strict";
import { cnNum, errorLabel } from "../src/util.ts";

const cases = {
  0: "〇", 3: "三", 10: "十", 11: "十一", 19: "十九", 20: "二十", 25: "二十五", 99: "九十九",
  100: "一百", 101: "一百〇一", 105: "一百〇五", 110: "一百一十", 112: "一百一十二", 120: "一百二十", 200: "二百", 999: "九百九十九",
  1000: "一千", 1005: "一千〇五", 1010: "一千〇一十", 1100: "一千一百", 1234: "一千二百三十四", 9999: "九千九百九十九", 10000: "10000",
};
for (const [n, cn] of Object.entries(cases)) assert.equal(cnNum(+n), cn, n);

assert.equal(errorLabel("CONTENT_SAFETY"), "被内容安全过滤拦下");
assert.equal(errorLabel("NETWORK"), "网络中断");
for (const kind of ["UNKNOWN", "KeyError", "", null, undefined]) assert.equal(errorLabel(kind), "未分类");
console.log("ok");
