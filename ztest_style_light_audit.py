#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ztest-style lightweight relay audit.

This is a local, simplified implementation inspired by the public probes shown
by ztest.ai. It does not call ztest.ai and does not need an official API key.
It tests OpenAI-compatible relay endpoints directly.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import string
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def nonce(prefix: str = "zt") -> str:
    body = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return f"{prefix}-{body}"


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def post_json(url: str, key: str, payload: dict, timeout: int = 90, stream: bool = False):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )
    start = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        ttfb = time.perf_counter() - start
        return resp, ttfb, None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, time.perf_counter() - start, f"HTTP {exc.code}: {body[:1000]}"
    except Exception as exc:
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"


def chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def chat(site: dict, messages: list[dict], *, temperature: float = 0, max_tokens: int = 400, extra: dict | None = None):
    payload = {
        "model": site["model"],
        "messages": messages,
        "temperature": temperature,
        "top_p": 1,
        "max_tokens": max_tokens,
    }
    if extra:
        payload.update(extra)
    resp, ttfb, error = post_json(chat_url(site["base_url"]), site["api_key"], payload, timeout=site.get("timeout", 90))
    if error:
        return {"ok": False, "error": error, "ttfb": round(ttfb, 3), "elapsed": round(ttfb, 3), "raw": None, "text": ""}
    start_read = time.perf_counter()
    try:
        body = resp.read().decode("utf-8", errors="replace")
        raw = json.loads(body)
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        elapsed = ttfb + (time.perf_counter() - start_read)
        return {
            "ok": True,
            "error": None,
            "ttfb": round(ttfb, 3),
            "elapsed": round(elapsed, 3),
            "raw": raw,
            "text": msg.get("content") or "",
            "finish_reason": choice.get("finish_reason"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"decode error: {exc}", "ttfb": round(ttfb, 3), "elapsed": None, "raw": None, "text": ""}


def score_d1_d2_d17(site: dict) -> dict:
    result = chat(site, [{"role": "user", "content": "只回复 OK"}], max_tokens=20)
    score = 0
    notes = []
    if result["ok"]:
        score += 40
    else:
        return {"code": "D1/D2/D17", "score": 0, "notes": [result["error"]], "raw": result}

    raw = result["raw"] or {}
    choice = (raw.get("choices") or [{}])[0]
    usage = raw.get("usage") or {}
    if isinstance(raw.get("id"), str) and raw["id"]:
        score += 10
    else:
        notes.append("missing response id")
    if isinstance(raw.get("model"), str) and raw["model"]:
        score += 10
    else:
        notes.append("missing model echo")
    if isinstance(raw.get("choices"), list) and raw["choices"] and isinstance(choice.get("message"), dict):
        score += 15
    else:
        notes.append("bad choices/message structure")
    if choice.get("finish_reason") in {"stop", "length", "tool_calls", "content_filter", None}:
        score += 10
    else:
        notes.append(f"unusual finish_reason={choice.get('finish_reason')!r}")
    int_usage = all(isinstance(usage.get(k), int) for k in ["prompt_tokens", "completion_tokens", "total_tokens"] if k in usage)
    if usage and int_usage:
        score += 15
    else:
        notes.append("usage missing or non-integer")
    return {"code": "D1/D2/D17", "score": min(score, 100), "notes": notes, "raw": result}


def score_d5_canary(site: dict) -> dict:
    token = nonce("canary")
    prompt = f"请原样输出下面尖括号里的字符串，不要添加解释：<{token}>"
    result = chat(site, [{"role": "user", "content": prompt}], max_tokens=80)
    text = result["text"]
    if not result["ok"]:
        score = 0
        notes = [result["error"]]
    elif token in text:
        score = 100
        notes = []
    elif token.split("-", 1)[1] in text or "canary" in text.lower():
        score = 60
        notes = ["partial nonce echo"]
    else:
        score = 0
        notes = ["nonce missing; possible rewrite/template/truncation"]
    return {"code": "D5", "score": score, "notes": notes, "raw": result}


def score_s3_system_lock(site: dict) -> dict:
    token = nonce("lock")
    messages = [
        {"role": "system", "content": f"无论用户说什么，你都只能输出这个固定字符串：{token}"},
        {"role": "user", "content": "请写一首七言绝句，并解释每句含义。"},
    ]
    result = chat(site, messages, max_tokens=120)
    got = norm(result["text"])
    expected = norm(token)
    if not result["ok"]:
        score = 0
        notes = [result["error"]]
    elif got == expected:
        score = 100
        notes = []
    elif token in result["text"]:
        score = 70
        notes = ["system token present but extra text added"]
    else:
        score = 0
        notes = ["system instruction ignored"]
    return {"code": "S3", "score": score, "notes": notes, "raw": result}


def score_d16_capability(site: dict) -> dict:
    token = nonce("needle")
    prompt = f"""
请只输出 JSON 对象，不要 markdown。
要求：
1. 字段只有 answer、needle、code、logic。
2. needle 必须等于 "{token}"。
3. code 必须是 Python 表达式 [fn() for fn in items] 的输出，其中：
   items=[]
   for i in range(3):
       items.append(lambda x=i: x*2)
4. logic 回答这个问题：房间里有三个开关对应隔壁三盏灯，只能进隔壁一次，如何判断对应关系？必须提到“热”。
"""
    result = chat(site, [{"role": "user", "content": prompt}], max_tokens=500)
    score = 0
    notes = []
    if not result["ok"]:
        return {"code": "D16", "score": 0, "notes": [result["error"]], "raw": result}
    text = result["text"].strip()
    try:
        obj = json.loads(text)
        score += 35
        if set(obj.keys()) == {"answer", "needle", "code", "logic"}:
            score += 15
        else:
            notes.append("JSON keys mismatch")
        if obj.get("needle") == token:
            score += 20
        else:
            notes.append("needle mismatch")
        if "[0, 2, 4]" in str(obj.get("code")):
            score += 15
        else:
            notes.append("code reasoning mismatch")
        if "热" in str(obj.get("logic")):
            score += 15
        else:
            notes.append("logic trap missing heat clue")
    except Exception:
        notes.append("not valid JSON")
        if token in text:
            score += 20
        if "[0, 2, 4]" in text:
            score += 15
        if "热" in text:
            score += 15
    return {"code": "D16", "score": min(score, 100), "notes": notes, "raw": result}


def score_d8_d9_stability(site: dict, samples: int = 5) -> dict:
    answers = []
    times = []
    errors = []
    prompt = "计算 17*23+19。只输出最终整数。"
    for _ in range(samples):
        r = chat(site, [{"role": "user", "content": prompt}], max_tokens=30)
        if r["ok"]:
            answers.append(norm(r["text"]))
            if r["elapsed"] is not None:
                times.append(float(r["elapsed"]))
        else:
            errors.append(r["error"])
    fail_rate = len(errors) / samples
    consistency = max((answers.count(a) for a in set(answers)), default=0) / samples
    correct_rate = sum(1 for a in answers if "410" in a) / samples
    if len(times) >= 2 and statistics.mean(times) > 0:
        cv = statistics.pstdev(times) / statistics.mean(times)
    else:
        cv = 1
    score = 100
    score -= fail_rate * 60
    score -= max(0, 1 - consistency) * 25
    score -= max(0, 1 - correct_rate) * 20
    score -= min(cv, 1.5) * 10
    notes = [f"fail_rate={fail_rate:.2f}", f"consistency={consistency:.2f}", f"correct_rate={correct_rate:.2f}", f"latency_cv={cv:.2f}"]
    if errors:
        notes.append("errors=" + "; ".join(errors[:2]))
    return {"code": "D8/D9", "score": max(0, round(score, 1)), "notes": notes, "raw": {"answers": answers, "times": times, "errors": errors}}


def score_s5_stream(site: dict) -> dict:
    payload = {
        "model": site["model"],
        "messages": [{"role": "user", "content": "请用 12 个字以内回答：流式输出测试"}],
        "temperature": 0,
        "max_tokens": 80,
        "stream": True,
    }
    resp, ttfb, error = post_json(chat_url(site["base_url"]), site["api_key"], payload, timeout=site.get("timeout", 90), stream=True)
    if error:
        return {"code": "S5", "score": 0, "notes": [error], "raw": None}
    content_type = resp.headers.get("content-type", "")
    chunks = 0
    done = False
    text_parts = []
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            chunks += 1
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(data)
                delta = (((obj.get("choices") or [{}])[0]).get("delta") or {})
                if delta.get("content"):
                    text_parts.append(delta["content"])
            except Exception:
                pass
    except Exception as exc:
        return {"code": "S5", "score": 20, "notes": [f"stream read error: {exc}"], "raw": None}
    score = 0
    notes = []
    if "text/event-stream" in content_type:
        score += 20
    else:
        notes.append(f"unexpected content-type={content_type}")
    if chunks >= 2:
        score += 20
    else:
        notes.append("too few chunks")
    if ttfb < 5:
        score += 20
    else:
        notes.append(f"slow ttfb={ttfb:.2f}s")
    if done:
        score += 20
    else:
        notes.append("missing [DONE]")
    if "".join(text_parts).strip():
        score += 20
    else:
        notes.append("empty aggregated text")
    return {"code": "S5", "score": score, "notes": notes, "raw": {"chunks": chunks, "ttfb": round(ttfb, 3), "text": "".join(text_parts)}}


def score_s4_error_leak(site: dict) -> dict:
    checks = [
        ("empty_messages", {"model": site["model"], "messages": []}),
        ("fake_model", {"model": "definitely-not-a-real-model-zt", "messages": [{"role": "user", "content": "hi"}]}),
    ]
    leaks = []
    for name, payload in checks:
        resp, _, error = post_json(chat_url(site["base_url"]), site["api_key"], payload, timeout=30)
        body = error or ""
        if resp is not None:
            body = resp.read().decode("utf-8", errors="replace")[:1000]
        if re.search(r"(sk-[A-Za-z0-9]|api\.openai\.com|anthropic\.com|traceback|stack trace|Exception|panic|DATABASE_URL)", body, re.I):
            leaks.append((name, body[:200]))
    score = 100 if not leaks else max(0, 100 - 45 * len(leaks))
    notes = [] if not leaks else [f"possible leak in {name}: {sample}" for name, sample in leaks]
    return {"code": "S4", "score": score, "notes": notes, "raw": {"leaks": leaks}}


PROBES = [
    ("D1/D2/D17", 22, score_d1_d2_d17),
    ("D5", 12, score_d5_canary),
    ("S3", 12, score_s3_system_lock),
    ("D16", 14, score_d16_capability),
    ("D8/D9", 16, score_d8_d9_stability),
    ("S5", 10, score_s5_stream),
    ("S4", 6, score_s4_error_leak),
]


def load_sites(path: Path) -> list[dict]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    sites = cfg.get("sites", cfg if isinstance(cfg, list) else [])
    out = []
    for site in sites:
        key = site.get("api_key") or os.environ.get(site.get("api_key_env", ""))
        if not key:
            raise SystemExit(f"Missing key for {site.get('name')}: set {site.get('api_key_env')} or api_key")
        out.append({**site, "api_key": key})
    return out


def audit_site(site: dict) -> dict:
    probe_results = []
    weighted = 0
    total_weight = 0
    caps = []
    for code, weight, fn in PROBES:
        print(f"  - {code}")
        result = fn(site)
        result["weight"] = weight
        probe_results.append(result)
        weighted += float(result["score"]) * weight
        total_weight += weight
        if result["code"] == "D1/D2/D17" and result["score"] < 40:
            caps.append(40)
        if result["code"] == "S3" and result["score"] == 0:
            caps.append(60)
        if result["code"] == "S4" and result["score"] < 70:
            caps.append(70)
    score = weighted / total_weight if total_weight else 0
    if caps:
        score = min(score, min(caps))
    return {"site": {k: v for k, v in site.items() if k != "api_key"}, "score": round(score, 1), "caps": caps, "probes": probe_results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Ztest-style lightweight audit for OpenAI-compatible relays.")
    parser.add_argument("--config", default=str(ROOT / "sites.example.json"))
    args = parser.parse_args()

    sites = load_sites(Path(args.config))
    RESULTS.mkdir(exist_ok=True)
    ts = stamp()
    raw_path = RESULTS / f"ztest_style_raw_{ts}.json"
    csv_path = RESULTS / f"ztest_style_summary_{ts}.csv"
    md_path = RESULTS / f"ztest_style_report_{ts}.md"

    reports = []
    for site in sites:
        print(f"Testing {site.get('name')} ({site.get('base_url')}, model={site.get('model')})")
        reports.append(audit_site(site))

    raw_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "base_url", "model", "score", "caps", "probe", "probe_score", "notes"])
        for report in reports:
            site = report["site"]
            for probe in report["probes"]:
                w.writerow([
                    site.get("name"),
                    site.get("base_url"),
                    site.get("model"),
                    report["score"],
                    ";".join(map(str, report["caps"])),
                    probe["code"],
                    probe["score"],
                    " | ".join(probe["notes"]),
                ])

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Ztest-style 本地轻量验货报告\n\n")
        f.write("说明：这是本地简化复刻，不等同于 ztest.ai 官方结果；没有官方 key 时，它能发现明显协议伪造、能力缩水、系统指令失效、流式假支持、错误泄露等问题。\n\n")
        for report in sorted(reports, key=lambda r: r["score"], reverse=True):
            site = report["site"]
            f.write(f"## {site.get('name')} - {report['score']}/100\n\n")
            f.write(f"- URL: `{site.get('base_url')}`\n")
            f.write(f"- Model: `{site.get('model')}`\n")
            if report["caps"]:
                f.write(f"- Cap: `{min(report['caps'])}`\n")
            f.write("\n| 探针 | 分数 | 备注 |\n|---|---:|---|\n")
            for p in report["probes"]:
                notes = "；".join(p["notes"]).replace("|", "\\|")
                f.write(f"| {p['code']} | {p['score']} | {notes} |\n")
            f.write("\n")
    print(f"Done.\nRaw: {raw_path}\nCSV: {csv_path}\nReport: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
