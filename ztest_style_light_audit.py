#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ztest_style_light_audit.py — 本地中转验货 CLI（瘦封装，逻辑都在 audit_core）。

两种模式：
  默认       ：对配置里每个站点各自独立跑全套探针并打分。
  --compare  ：取配置里前两个站点做双中转横评 + 一致性交叉校验 + 对比结论。

不依赖任何第三方库。key 通过环境变量或 sites.json 的 api_key 传入。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

import audit_core

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def load_sites(path: Path) -> list[dict]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    sites = cfg.get("sites", cfg if isinstance(cfg, list) else [])
    out = []
    for site in sites:
        key = site.get("api_key") or os.environ.get(site.get("api_key_env", ""))
        if not key:
            raise SystemExit(f"缺少 key：{site.get('name')} —— 请设置环境变量 {site.get('api_key_env')} 或填 api_key")
        site = {**site, "api_key": key}
        site.setdefault("protocol", "openai")
        out.append(site)
    return out


def run_independent(sites: list[dict], samples: int) -> int:
    RESULTS.mkdir(exist_ok=True)
    ts = audit_core.stamp()
    raw_path = RESULTS / f"ztest_style_raw_{ts}.json"
    csv_path = RESULTS / f"ztest_style_summary_{ts}.csv"
    md_path = RESULTS / f"ztest_style_report_{ts}.md"

    reports = []
    for site in sites:
        print(f"测试 {site.get('name')} [{site.get('protocol')}] ({site.get('base_url')}, model={site.get('model')})")
        reports.append(audit_core.run_audit(site, samples=samples))

    raw_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "protocol", "base_url", "model", "score", "caps", "probe", "probe_score", "notes"])
        for report in reports:
            s = report["site"]
            for p in report["probes"]:
                w.writerow([s.get("name"), s.get("protocol"), s.get("base_url"), s.get("model"),
                            report["score"], ";".join(map(str, report["caps"])),
                            p["code"], p["score"], " | ".join(p["notes"])])

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# 本地轻量验货报告\n\n")
        f.write("说明：本地简化复刻，不等同官方结论；无官方 key 时可发现明显协议伪造、能力缩水、系统指令失效、流式假支持、错误泄露等问题。\n\n")
        for report in sorted(reports, key=lambda r: r["score"], reverse=True):
            s = report["site"]
            f.write(f"## {s.get('name')} - {report['score']}/100 （{report['band']}）\n\n")
            f.write(f"- 协议：`{s.get('protocol')}`\n- URL：`{s.get('base_url')}`\n- Model：`{s.get('model')}`\n")
            if report["model_echo"]:
                f.write(f"- 自报模型：`{report['model_echo']}`\n")
            if report["caps"]:
                f.write(f"- 封顶：`{min(report['caps'])}`\n")
            f.write("\n| 探针 | 分数 | 备注 |\n|---|---:|---|\n")
            for p in report["probes"]:
                notes = "；".join(p["notes"]).replace("|", "\\|")
                f.write(f"| {p['title']} ({p['code']}) | {p['score']} | {notes} |\n")
            f.write("\n")
    print(f"完成。\nRaw: {raw_path}\nCSV: {csv_path}\nReport: {md_path}")
    return 0


def run_compare(sites: list[dict], samples: int) -> int:
    if len(sites) < 2:
        raise SystemExit("--compare 需要配置里至少两个站点")
    a, b = sites[0], sites[1]
    print(f"横评：{a.get('name')} [{a.get('protocol')}]  VS  {b.get('name')} [{b.get('protocol')}]")
    result = audit_core.compare(a, b, samples=samples)

    RESULTS.mkdir(exist_ok=True)
    ts = audit_core.stamp()
    raw_path = RESULTS / f"compare_raw_{ts}.json"
    md_path = RESULTS / f"compare_report_{ts}.md"
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    v, ra, rb, cr = result["verdict"], result["report_a"], result["report_b"], result["cross"]
    amap = {p["code"]: p for p in ra["probes"]}
    bmap = {p["code"]: p for p in rb["probes"]}
    with md_path.open("w", encoding="utf-8") as f:
        f.write("# 双中转横评报告\n\n")
        f.write(f"- 生成时间：{result['generated_at']}\n")
        f.write(f"- A：{v['name_a']} → **{v['score_a']}/100**（{v['band_a']}）\n")
        f.write(f"- B：{v['name_b']} → **{v['score_b']}/100**（{v['band_b']}）\n")
        reco = "基本持平" if v["recommended"] == "基本持平" else f"综合更优：{v['recommended']}"
        f.write(f"- 结论：{reco}\n")
        f.write(f"- 确定性题一致率：{int(cr['agreement_rate']*100)}%，中位相似度：{cr['similarity_median']}\n\n")
        if v["flags"]:
            f.write("## 可疑信号\n\n")
            for fl in v["flags"]:
                f.write(f"- {fl}\n")
            f.write("\n")
        f.write("## 分项对比\n\n| 探针 | A | B | 差值 |\n|---|---:|---:|---:|\n")
        for code, _, _ in audit_core.PROBE_DEFS:
            pa, pb = amap.get(code), bmap.get(code)
            if not pa and not pb:
                continue
            sa = pa["score"] if pa else "-"
            sb = pb["score"] if pb else "-"
            delta = round(pa["score"] - pb["score"], 1) if pa and pb else "-"
            title = (pa or pb)["title"]
            f.write(f"| {title} ({code}) | {sa} | {sb} | {delta} |\n")
        f.write("\n## 一致性逐题\n\n| 题目 | 一致 | 相似度 | A 正确 | B 正确 |\n|---|---|---:|---|---|\n")
        for r in cr["rows"]:
            f.write(f"| {r['cid']} | {'是' if r['agree'] else '否'} | {r['similarity']} | {r['a_correct']} | {r['b_correct']} |\n")
    print(f"完成。\nRaw: {raw_path}\nReport: {md_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地中转验货 CLI（OpenAI / Anthropic）")
    parser.add_argument("--config", default=str(ROOT / "sites.example.json"))
    parser.add_argument("--compare", action="store_true", help="取前两个站点做双中转横评")
    parser.add_argument("--samples", type=int, default=5, help="稳定性探针采样次数")
    args = parser.parse_args()

    sites = load_sites(Path(args.config))
    if args.compare:
        return run_compare(sites, args.samples)
    return run_independent(sites, args.samples)


if __name__ == "__main__":
    raise SystemExit(main())
