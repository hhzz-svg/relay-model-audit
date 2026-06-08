#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI-compatible relay model audit.

Reads credentials from environment variables only. It sends the same prompts to
an official endpoint and a relay endpoint, then writes raw outputs and a compact
report under ./results.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
from pathlib import Path
import re
import statistics
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


DEFAULT_OFFICIAL_BASE_URL = "https://api.openai.com/v1"


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def post_json(url: str, api_key: str, payload: dict, timeout: int) -> tuple[dict, float]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - start
            return json.loads(body), elapsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        elapsed = time.perf_counter() - start
        raise RuntimeError(f"HTTP {exc.code}: {body[:1200]}") from exc
    except Exception as exc:
        elapsed = time.perf_counter() - start
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def chat_once(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "top_p": 1,
        "max_tokens": max_tokens,
    }
    url = f"{normalize_base_url(base_url)}/chat/completions"
    try:
        response, elapsed = post_json(url, api_key, payload, timeout)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = response.get("usage") or {}
        return {
            "ok": True,
            "elapsed_sec": round(elapsed, 3),
            "text": text,
            "finish_reason": choice.get("finish_reason"),
            "usage": usage,
            "model_returned": response.get("model"),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_sec": None,
            "text": "",
            "finish_reason": None,
            "usage": {},
            "model_returned": None,
            "error": str(exc),
        }


def load_prompts(path: Path) -> list[dict]:
    tests = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            item = json.loads(line)
            item.setdefault("id", f"case_{line_no}")
            item.setdefault("category", "general")
            item.setdefault("system", "You are a careful assistant. Follow the user request exactly.")
            item.setdefault("max_tokens", 800)
            tests.append(item)
    return tests


def text_features(text: str) -> dict:
    stripped = text.strip()
    return {
        "chars": len(stripped),
        "lines": stripped.count("\n") + (1 if stripped else 0),
        "has_json_fence": "```json" in stripped.lower(),
        "has_apology": bool(re.search(r"\b(sorry|apologize|抱歉|对不起)\b", stripped, re.I)),
        "has_chinese": bool(re.search(r"[\u4e00-\u9fff]", stripped)),
    }


def similarity(a: str, b: str) -> float:
    if not a.strip() and not b.strip():
        return 1.0
    return round(difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio(), 4)


def pass_rule(rule: str | None, text: str) -> tuple[bool | None, str]:
    if not rule:
        return None, ""
    if rule.startswith("contains:"):
        needle = rule[len("contains:") :]
        return needle in text, f"contains {needle!r}"
    if rule.startswith("regex:"):
        pattern = rule[len("regex:") :]
        return bool(re.search(pattern, text, re.S)), f"regex {pattern!r}"
    if rule == "json_object":
        try:
            json.loads(text)
            return True, "valid JSON object"
        except Exception:
            return False, "valid JSON object"
    return None, f"unknown rule {rule!r}"


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit an OpenAI-compatible relay against an official endpoint.")
    parser.add_argument("--prompts", default=str(ROOT / "prompts.jsonl"), help="JSONL prompt file")
    parser.add_argument("--official-base-url", default=env("OFFICIAL_BASE_URL", DEFAULT_OFFICIAL_BASE_URL))
    parser.add_argument("--official-model", default=env("OFFICIAL_MODEL"))
    parser.add_argument("--relay-base-url", default=env("RELAY_BASE_URL"))
    parser.add_argument("--relay-model", default=env("RELAY_MODEL"))
    parser.add_argument("--temperature", type=float, default=float(env("AUDIT_TEMPERATURE", "0")))
    parser.add_argument("--timeout", type=int, default=int(env("AUDIT_TIMEOUT", "90")))
    parser.add_argument("--repeat", type=int, default=int(env("AUDIT_REPEAT", "1")))
    args = parser.parse_args()

    official_key = env("OFFICIAL_API_KEY")
    relay_key = env("RELAY_API_KEY")

    missing = []
    for name, value in [
        ("OFFICIAL_API_KEY", official_key),
        ("OFFICIAL_MODEL", args.official_model),
        ("RELAY_API_KEY", relay_key),
        ("RELAY_BASE_URL", args.relay_base_url),
        ("RELAY_MODEL", args.relay_model),
    ]:
        if not value:
            missing.append(name)
    if missing:
        print("Missing required settings: " + ", ".join(missing))
        print("See README.md and .env.example.")
        return 2

    prompts = load_prompts(Path(args.prompts))
    RESULTS.mkdir(exist_ok=True)
    stamp = now_stamp()
    raw_path = RESULTS / f"audit_raw_{stamp}.jsonl"
    csv_path = RESULTS / f"audit_summary_{stamp}.csv"
    md_path = RESULTS / f"audit_report_{stamp}.md"

    rows = []
    with raw_path.open("w", encoding="utf-8") as raw:
        for repeat in range(args.repeat):
            for case in prompts:
                print(f"[{repeat + 1}/{args.repeat}] {case['id']} {case['category']}")
                official = chat_once(
                    base_url=args.official_base_url,
                    api_key=official_key,
                    model=args.official_model,
                    system=case["system"],
                    prompt=case["prompt"],
                    temperature=args.temperature,
                    max_tokens=int(case.get("max_tokens", 800)),
                    timeout=args.timeout,
                )
                relay = chat_once(
                    base_url=args.relay_base_url,
                    api_key=relay_key,
                    model=args.relay_model,
                    system=case["system"],
                    prompt=case["prompt"],
                    temperature=args.temperature,
                    max_tokens=int(case.get("max_tokens", 800)),
                    timeout=args.timeout,
                )
                off_pass, off_rule = pass_rule(case.get("pass_rule"), official["text"])
                rel_pass, rel_rule = pass_rule(case.get("pass_rule"), relay["text"])
                record = {
                    "repeat": repeat,
                    "case": case,
                    "official": official,
                    "relay": relay,
                    "similarity": similarity(official["text"], relay["text"]),
                    "official_features": text_features(official["text"]),
                    "relay_features": text_features(relay["text"]),
                    "official_pass": off_pass,
                    "relay_pass": rel_pass,
                    "pass_rule": off_rule or rel_rule,
                }
                raw.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows.append(record)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "case_id",
            "category",
            "official_ok",
            "relay_ok",
            "official_pass",
            "relay_pass",
            "similarity",
            "official_elapsed_sec",
            "relay_elapsed_sec",
            "official_chars",
            "relay_chars",
            "relay_error",
        ])
        for r in rows:
            writer.writerow([
                r["case"]["id"],
                r["case"]["category"],
                r["official"]["ok"],
                r["relay"]["ok"],
                r["official_pass"],
                r["relay_pass"],
                r["similarity"],
                r["official"]["elapsed_sec"],
                r["relay"]["elapsed_sec"],
                r["official_features"]["chars"],
                r["relay_features"]["chars"],
                r["relay"]["error"] or "",
            ])

    official_ok = sum(1 for r in rows if r["official"]["ok"])
    relay_ok = sum(1 for r in rows if r["relay"]["ok"])
    comparable = [r for r in rows if r["official"]["ok"] and r["relay"]["ok"]]
    sim_values = [r["similarity"] for r in comparable]
    off_times = [r["official"]["elapsed_sec"] for r in rows if r["official"]["elapsed_sec"] is not None]
    rel_times = [r["relay"]["elapsed_sec"] for r in rows if r["relay"]["elapsed_sec"] is not None]
    rule_rows = [r for r in rows if r["official_pass"] is not None or r["relay_pass"] is not None]
    off_passes = sum(1 for r in rule_rows if r["official_pass"] is True)
    rel_passes = sum(1 for r in rule_rows if r["relay_pass"] is True)
    suspect = []
    if relay_ok < official_ok:
        suspect.append("relay error count is higher than official")
    if rule_rows and rel_passes + 2 <= off_passes:
        suspect.append("relay failed noticeably more pass rules")
    if comparable and median(sim_values) is not None and median(sim_values) < 0.12:
        suspect.append("median output similarity is very low")
    if comparable:
        short_count = sum(
            1
            for r in comparable
            if r["relay_features"]["chars"] < max(80, r["official_features"]["chars"] * 0.35)
        )
        if short_count >= max(2, len(comparable) // 3):
            suspect.append("relay responses are often much shorter")

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# 模型中转站验货报告\n\n")
        f.write(f"- 时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 官方端：`{args.official_base_url}` / `{args.official_model}`\n")
        f.write(f"- 中转端：`{args.relay_base_url}` / `{args.relay_model}`\n")
        f.write(f"- 用例数：{len(prompts)}，重复次数：{args.repeat}\n")
        f.write(f"- 官方成功：{official_ok}/{len(rows)}；中转成功：{relay_ok}/{len(rows)}\n")
        f.write(f"- 中位相似度：{median(sim_values)}\n")
        f.write(f"- 官方中位延迟：{median(off_times)} 秒；中转中位延迟：{median(rel_times)} 秒\n")
        if rule_rows:
            f.write(f"- 规则通过：官方 {off_passes}/{len(rule_rows)}；中转 {rel_passes}/{len(rule_rows)}\n")
        f.write("\n## 初步判断\n\n")
        if suspect:
            f.write("发现可疑信号：\n\n")
            for item in suspect:
                f.write(f"- {item}\n")
        else:
            f.write("未发现强可疑信号，但这不等于证明中转端一定没有替换模型。建议增加更多私有题和长上下文题继续测。\n")
        f.write("\n## 逐题摘要\n\n")
        f.write("| 用例 | 类别 | 官方 | 中转 | 规则通过 | 相似度 | 中转错误 |\n")
        f.write("|---|---|---:|---:|---|---:|---|\n")
        for r in rows:
            relay_error = (r["relay"]["error"] or "").replace("|", "\\|")[:120]
            f.write(
                f"| {r['case']['id']} | {r['case']['category']} | "
                f"{'OK' if r['official']['ok'] else 'ERR'} | {'OK' if r['relay']['ok'] else 'ERR'} | "
                f"{r['official_pass']} / {r['relay_pass']} | {r['similarity']} | {relay_error} |\n"
            )
        f.write("\n## 输出文件\n\n")
        f.write(f"- 原始结果：`{raw_path.name}`\n")
        f.write(f"- CSV 汇总：`{csv_path.name}`\n")

    print(f"Done.\nRaw: {raw_path}\nCSV: {csv_path}\nReport: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
