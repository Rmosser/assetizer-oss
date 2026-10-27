"""Benchmark report: what each limit measured this week, the change since the last report, and which workaround the
numbers say could go. Verdicts are suggestions; dropping a workaround stays a decision (docs/benchmarks.md)."""
from __future__ import annotations

import json
from pathlib import Path

from ..media.ffmpeg import VIEW_FILE_LIMIT


def verdicts(r: dict) -> list[tuple[str, str]]:
    """(constraint, what the numbers say) for the constraints this run measured."""
    out = []
    ms = (r.get("probes") or {}).get("media_size")
    if ms:
        mb = ms.get("largest_ok_mb")
        limit = VIEW_FILE_LIMIT // (1024 * 1024)
        if mb and mb > limit:
            out.append(("C1", f"收得下 {mb} MB 的视频（现限 {limit} MB）：可以放宽代理大小上限，少压几档"))
        else:
            out.append(("C1", f"最大只到 {mb or 0} MB：代理大小上限维持 {limit} MB"))
    ol = (r.get("probes") or {}).get("output_length")
    if ol:
        e, m = ol.get("enforced") or {}, ol.get("embedded") or {}
        if e.get("complete") or m.get("complete"):
            best = max(e.get("output_tokens") or 0, m.get("output_tokens") or 0)
            out.append(("C4", f"一次完整输出了 {ol['items_asked']} 条（约 {best} token）：可以评估取消分批生成"))
        else:
            out.append(("C4", "一次输出不完整：继续分批生成"))
        out.append(("C5", "强制 schema 下的大输出完整：大输出可以改用强制 schema" if e.get("complete")
                    else "强制 schema 下的大输出不完整：大输出继续把 schema 写进提示词"))
    for gid, q in (r.get("quality") or {}).items():
        w, c = (q.get("variants") or {}).get("whole") or {}, (q.get("variants") or {}).get("chunked") or {}
        if "recall" in w and "recall" in c:
            close = w["recall"] >= c["recall"] - 0.05 and (w["start_error_median"] or 1e9) <= 1.2 * (c["start_error_median"] or 1e9)
            out.append(("C2/C3", f"{gid}：整段召回 {w['recall']:.0%}、时间误差中位 {w['start_error_median']} 秒；"
                                 f"分段 {c['recall']:.0%}、{c['start_error_median']} 秒 → "
                                 + ("整段已追平分段，可以评估取消分段" if close else "整段仍明显落后，继续分段")))
    return out


def _previous(reports: Path, stamp: str) -> dict | None:
    older = sorted(p for p in reports.glob("*.json") if p.stem < stamp)
    return json.loads(older[-1].read_text()) if older else None


def markdown(r: dict, prev: dict | None) -> str:
    lines = [f"# 基准报告 {r['stamp']}", "", f"Provider：`{r['provider']}`" + (f"；对比上次 {prev['stamp']}" if prev else "；首次运行"), ""]
    lines += ["## 结论", "", "| 限制 | 本次数据说明 |", "|---|---|"]
    lines += [f"| {c} | {v} |" for c, v in verdicts(r)] + [""]
    ms = (r.get("probes") or {}).get("media_size")
    if ms:
        lines += ["## C1 视频大小上限", "", "| 大小 | 结果 | 耗时 | 输入 token |", "|---|---|---|---|"]
        lines += [f"| {x['mb']} MB | {'通过' if x.get('ok') else x.get('error')} | {x.get('wall', '')} | {x.get('input_tokens', '')} |"
                  for x in ms["rows"]] + [""]
    ol = (r.get("probes") or {}).get("output_length")
    if ol:
        lines += [f"## C4/C5 单次输出（要求 {ol['items_asked']} 条）", "", "| 方式 | 完整 | 条数 | 输出 token | 耗时 | 错误 |",
                  "|---|---|---|---|---|---|"]
        for mode in ("enforced", "embedded"):
            x = ol.get(mode) or {}
            lines.append(f"| {mode} | {'是' if x.get('complete') else '否'} | {x.get('items', '')} | {x.get('output_tokens', '')} "
                         f"| {x.get('wall', '')} | {x.get('error') or x.get('parse_error') or ''} |")
        lines.append("")
    for gid, q in (r.get("quality") or {}).items():
        lines += [f"## C2/C3 整段 vs 分段理解：{gid}（{q['units']} 个参照单元）", "",
                  "| 方式 | 召回 | 时间误差中位（秒） | ≤60 秒 | 主题数 | 调用 | 输入 token | 耗时 | 代理编码 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        pv = ((prev or {}).get("quality") or {}).get(gid, {}).get("variants", {})
        for name in ("whole", "chunked"):
            x = q["variants"].get(name) or {}
            if "error" in x:
                lines.append(f"| {name} | 失败：{x['error']} | | | | | | | |")
                continue
            was = pv.get(name, {}).get("recall")
            delta = f"（上次 {was:.0%}）" if isinstance(was, (int, float)) else ""
            lines.append(f"| {name} | {x['recall']:.0%}{delta} | {x['start_error_median']} | {x['within_60s']:.0%} | {x['topics']} "
                         f"| {x['calls']} | {x['input_tokens']} | {round(x['wall'])} | {x.get('proxy')} |")
        lines += ["", "参照单元来自 Run-014 的整体验收产出，不是逐条核对的标准答案：数字说明整段与分段的差距，"
                  "不是绝对准确率。", ""]
    return "\n".join(lines)


def write(r: dict, reports: Path) -> dict:
    reports.mkdir(parents=True, exist_ok=True)
    prev = _previous(reports, r["stamp"])
    (reports / f"{r['stamp']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    (reports / f"{r['stamp']}.md").write_text(markdown(r, prev))
    return {**r, "report": str(reports / f"{r['stamp']}.md")}
