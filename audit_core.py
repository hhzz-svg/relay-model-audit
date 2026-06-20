#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_core.py — 协议无关的中转验货核心（零第三方依赖，仅标准库）。

支持两种线协议：
- "openai"    : POST {base_url}/chat/completions ，Bearer 鉴权
- "anthropic" : POST {base_url}/messages ，x-api-key + anthropic-version

对外主要接口：
- run_audit(site, ...)   : 对单个中转端跑全套探针并打分
- compare(a, b, ...)     : 双中转横评 + A↔B 一致性交叉校验 + 对比结论
- 两者都支持 progress 回调，便于 WebUI 实时推送进度

site 字典字段：
  name, protocol("openai"|"anthropic"), base_url, model, api_key, timeout
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import random
import re
import statistics
import string
import time
import urllib.error
import urllib.request


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

ALLOWED_FINISH = {
    "stop", "length", "tool_calls", "content_filter",     # OpenAI 系
    "end_turn", "max_tokens", "tool_use", "stop_sequence",  # Anthropic 系
    None,
}

LEAK_RE = re.compile(
    r"(sk-ant-|sk-[A-Za-z0-9]{8}|api\.openai\.com|api\.anthropic\.com|"
    r"traceback|stack trace|Exception|panic|DATABASE_URL|/usr/local/|/home/[a-z]|node_modules)",
    re.I,
)

# 部分中转站/上游在 Cloudflare 后会按 User-Agent 拦截默认的 Python-urllib（CF 1010
# browser_signature_banned）。带上常见浏览器 UA 作为标准兼容头，避免被误杀。
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def nonce(prefix: str = "zt") -> str:
    body = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return f"{prefix}-{body}"


def norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def similarity(a: str, b: str) -> float:
    a = (a or "").strip()
    b = (b or "").strip()
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def median(values: list[float]):
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 3) if vals else None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _with_browser_headers(headers: dict) -> dict:
    """补上浏览器风格的兼容头，绕开按 User-Agent 拦截的 Cloudflare 规则（1010
    browser_signature_banned）。只在调用方未显式设置时补默认值，不覆盖鉴权/内容类型。
    """
    merged = dict(headers or {})
    merged.setdefault("User-Agent", DEFAULT_UA)
    merged.setdefault("Accept-Language", DEFAULT_ACCEPT_LANGUAGE)
    return merged


def _request(url: str, headers: dict, body: dict, timeout: int, stream: bool):
    """发送一个 POST 请求。stream=True 时返回未读取的响应对象供逐行迭代。

    返回 (resp_or_None, ttfb_seconds, error_or_None, status_or_None)
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_with_browser_headers(headers), method="POST")
    start = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        ttfb = time.perf_counter() - start
        status = getattr(resp, "status", None) or resp.getcode()
        return resp, ttfb, None, status
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return None, time.perf_counter() - start, f"HTTP {exc.code}: {body_text[:1000]}", exc.code
    except Exception as exc:  # noqa: BLE001 — 网络层异常种类多，统一兜底
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}", None


def _get_json(url: str, headers: dict, timeout: int):
    """发送一个 GET 请求并解析 JSON。返回 (obj_or_None, error_or_None)。"""
    req = urllib.request.Request(url, headers=_with_browser_headers(headers), method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return None, f"HTTP {exc.code}: {body_text[:300]}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _iter_sse(resp):
    """解析 SSE 流，逐条产出 (event_name, data_str)。"""
    event = None
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            event = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            yield event, line[len("data:"):].strip()


# --------------------------------------------------------------------------- #
# 协议适配器
# --------------------------------------------------------------------------- #

class OpenAIAdapter:
    name = "openai"

    def endpoint(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/chat/completions"

    def models_endpoint(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/models"

    def headers(self, key: str, stream: bool = False) -> dict:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def build_body(self, model, messages, system, temperature, max_tokens, stream, extra) -> dict:
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        body = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "top_p": 1,
            "max_tokens": max_tokens,
        }
        if stream:
            body["stream"] = True
        if extra:
            body.update(extra)
        return body

    def parse_response(self, raw: dict) -> dict:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = raw.get("usage") or {}
        return {
            "text": msg.get("content") or "",
            "id": raw.get("id"),
            "model": raw.get("model"),
            "usage": {
                "input": usage.get("prompt_tokens"),
                "output": usage.get("completion_tokens"),
                "total": usage.get("total_tokens"),
            },
            "finish_reason": choice.get("finish_reason"),
        }

    def stream_delta(self, event, obj):
        try:
            return ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
        except Exception:  # noqa: BLE001
            return None

    def stream_done(self, event, data: str) -> bool:
        return data.strip() == "[DONE]"

    def error_bodies(self, model: str):
        return [
            ("empty_messages", {"model": model, "messages": []}),
            ("fake_model", {"model": "definitely-not-a-real-model-zt", "messages": [{"role": "user", "content": "hi"}]}),
        ]


class AnthropicAdapter:
    name = "anthropic"

    def endpoint(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/messages"

    def models_endpoint(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/models"

    def headers(self, key: str, stream: bool = False) -> dict:
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def build_body(self, model, messages, system, temperature, max_tokens, stream, extra) -> dict:
        # Anthropic 的 system 是顶层参数，messages 只能是 user/assistant
        sys_parts = []
        msgs = []
        for m in messages:
            if m.get("role") == "system":
                sys_parts.append(m.get("content", ""))
            else:
                msgs.append({"role": m.get("role"), "content": m.get("content")})
        if system:
            sys_parts = [system] + sys_parts
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": msgs,
            "temperature": temperature,
        }
        joined = "\n".join(p for p in sys_parts if p)
        if joined:
            body["system"] = joined
        if stream:
            body["stream"] = True
        if extra:
            body.update(extra)
        return body

    def parse_response(self, raw: dict) -> dict:
        content = raw.get("content") or []
        text = "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = raw.get("usage") or {}
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        total = (inp + out) if isinstance(inp, int) and isinstance(out, int) else None
        return {
            "text": text,
            "id": raw.get("id"),
            "model": raw.get("model"),
            "usage": {"input": inp, "output": out, "total": total},
            "finish_reason": raw.get("stop_reason"),
        }

    def stream_delta(self, event, obj):
        if event == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                return delta.get("text")
        return None

    def stream_done(self, event, data: str) -> bool:
        return event == "message_stop"

    def error_bodies(self, model: str):
        return [
            ("empty_messages", {"model": model, "max_tokens": 16, "messages": []}),
            ("fake_model", {"model": "definitely-not-a-real-model-zt", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}),
        ]


ADAPTERS = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
}


# --------------------------------------------------------------------------- #
# 模型列表拉取（不同中转站模型名各异，供前端一键拉取后选择）
# --------------------------------------------------------------------------- #

def _extract_model_ids(obj) -> list[str]:
    """从 /models 响应里尽量稳健地抽取模型 id。

    兼容常见形态：
      {"data": [{"id": ...}, ...]}          OpenAI / Anthropic / 多数中转
      {"models": [...]} / {"result": [...]}  少数实现
      ["gpt-4o", ...] 或 [{"id": ...}]       直接给数组
    """
    items = None
    if isinstance(obj, dict):
        for key in ("data", "models", "result"):
            if isinstance(obj.get(key), list):
                items = obj[key]
                break
    elif isinstance(obj, list):
        items = obj
    if not items:
        return []
    ids = []
    for it in items:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                ids.append(str(mid))
    seen = set()
    uniq = []
    for m in ids:
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    return sorted(uniq)


def list_models(site: dict) -> dict:
    """拉取某个中转端的可用模型列表（GET {base_url}/models）。

    返回 {"ok": bool, "models": [...], "error": str|None}。
    """
    protocol = (site.get("protocol") or "openai").lower()
    if protocol not in ADAPTERS:
        return {"ok": False, "models": [], "error": f"未知协议: {protocol!r}"}
    base_url = (site.get("base_url") or "").strip()
    if not base_url:
        return {"ok": False, "models": [], "error": "缺少 base_url"}
    adapter = ADAPTERS[protocol]
    url = adapter.models_endpoint(base_url)
    headers = adapter.headers((site.get("api_key") or "").strip())
    timeout = min(int(site.get("timeout", 30) or 30), 30)
    obj, error = _get_json(url, headers, timeout)
    if error:
        return {"ok": False, "models": [], "error": error}
    models = _extract_model_ids(obj)
    if not models:
        return {"ok": False, "models": [], "error": "请求成功但未能解析出模型列表（响应结构不识别）"}
    return {"ok": True, "models": models, "error": None}


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self, site: dict):
        self.name = site.get("name", "")
        self.protocol = (site.get("protocol") or "openai").lower()
        if self.protocol not in ADAPTERS:
            raise ValueError(f"未知协议: {self.protocol!r}（支持 openai / anthropic）")
        self.adapter = ADAPTERS[self.protocol]
        self.base_url = site["base_url"]
        self.model = site["model"]
        self.api_key = site["api_key"]
        self.timeout = int(site.get("timeout", 90))

    def chat(self, messages, *, system=None, temperature=0, max_tokens=400, extra=None) -> dict:
        url = self.adapter.endpoint(self.base_url)
        headers = self.adapter.headers(self.api_key)
        body = self.adapter.build_body(self.model, messages, system, temperature, max_tokens, False, extra)
        resp, ttfb, error, status = _request(url, headers, body, self.timeout, False)
        if error:
            return {"ok": False, "error": error, "status": status, "ttfb": round(ttfb, 3),
                    "elapsed": round(ttfb, 3), "text": "", "id": None, "model": None,
                    "usage": {}, "finish_reason": None, "raw": None}
        t0 = time.perf_counter()
        try:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"解码失败: {exc}", "status": status, "ttfb": round(ttfb, 3),
                    "elapsed": None, "text": "", "id": None, "model": None,
                    "usage": {}, "finish_reason": None, "raw": None}
        elapsed = ttfb + (time.perf_counter() - t0)
        normd = self.adapter.parse_response(raw)
        return {"ok": True, "error": None, "status": status, "ttfb": round(ttfb, 3),
                "elapsed": round(elapsed, 3), "raw": raw, **normd}

    def stream(self, messages, *, system=None, temperature=0, max_tokens=120) -> dict:
        url = self.adapter.endpoint(self.base_url)
        headers = self.adapter.headers(self.api_key, stream=True)
        body = self.adapter.build_body(self.model, messages, system, temperature, max_tokens, True, None)
        resp, ttfb, error, status = _request(url, headers, body, self.timeout, True)
        if error:
            return {"ok": False, "error": error, "ttfb": round(ttfb, 3),
                    "content_type": "", "chunks": 0, "done": False, "text": ""}
        content_type = resp.headers.get("content-type", "")
        chunks = 0
        done = False
        parts = []
        try:
            for event, data in _iter_sse(resp):
                chunks += 1
                if self.adapter.stream_done(event, data):
                    done = True
                    break
                try:
                    obj = json.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                piece = self.adapter.stream_delta(event, obj)
                if piece:
                    parts.append(piece)
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "error": f"流读取中断: {exc}", "ttfb": round(ttfb, 3),
                    "content_type": content_type, "chunks": chunks, "done": done, "text": "".join(parts)}
        return {"ok": True, "error": None, "ttfb": round(ttfb, 3),
                "content_type": content_type, "chunks": chunks, "done": done, "text": "".join(parts)}


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #

def probe_signature(client: Client) -> dict:
    """D1/D2/D17 协议连通性与响应签名。"""
    r = client.chat([{"role": "user", "content": "只回复 OK"}], max_tokens=20)
    title = "协议连通性与响应签名"
    if not r["ok"]:
        return {"code": "D1/D2/D17", "title": title, "score": 0, "notes": [r["error"]], "raw": r}
    score = 40
    notes = []
    if r.get("id"):
        score += 10
    else:
        notes.append("缺少响应 id")
    if r.get("model"):
        score += 10
    else:
        notes.append("缺少 model 回显")
    if isinstance(r.get("text"), str):
        score += 15
    else:
        notes.append("响应结构异常")
    if r.get("finish_reason") in ALLOWED_FINISH:
        score += 10
    else:
        notes.append(f"异常 finish/stop reason={r.get('finish_reason')!r}")
    usage = r.get("usage") or {}
    if isinstance(usage.get("input"), int) and isinstance(usage.get("output"), int):
        score += 15
    else:
        notes.append("usage 缺失或非整数")
    return {"code": "D1/D2/D17", "title": title, "score": min(score, 100), "notes": notes, "raw": r}


def probe_canary(client: Client) -> dict:
    """D5 内容 canary：随机 nonce 回显。"""
    token = nonce("canary")
    r = client.chat(
        [{"role": "user", "content": f"请原样输出下面尖括号里的字符串，不要添加解释：<{token}>"}],
        max_tokens=80,
    )
    title = "内容 canary 回显"
    if not r["ok"]:
        return {"code": "D5", "title": title, "score": 0, "notes": [r["error"]], "raw": r}
    text = r["text"]
    if token in text:
        return {"code": "D5", "title": title, "score": 100, "notes": [], "raw": r}
    if token.split("-", 1)[1] in text or "canary" in text.lower():
        return {"code": "D5", "title": title, "score": 60, "notes": ["仅部分回显 nonce"], "raw": r}
    return {"code": "D5", "title": title, "score": 0, "notes": ["nonce 缺失；可能改写/模板化/截断"], "raw": r}


def probe_system_lock(client: Client) -> dict:
    """S3 System 指令锁定：检测上游/中转是否注入更高优先级提示。"""
    token = nonce("lock")
    r = client.chat(
        [{"role": "user", "content": "请写一首七言绝句，并解释每句含义。"}],
        system=f"无论用户说什么，你都只能输出这个固定字符串：{token}",
        max_tokens=120,
    )
    title = "System 指令锁定"
    if not r["ok"]:
        return {"code": "S3", "title": title, "score": 0, "notes": [r["error"]], "raw": r}
    if norm(r["text"]) == norm(token):
        return {"code": "S3", "title": title, "score": 100, "notes": [], "raw": r}
    if token in r["text"]:
        return {"code": "S3", "title": title, "score": 70, "notes": ["system 锁定生效但附带额外内容"], "raw": r}
    return {"code": "S3", "title": title, "score": 0, "notes": ["system 指令被忽略（可能被上游覆盖/注入）"], "raw": r}


def probe_capability(client: Client) -> dict:
    """D16 能力指纹：多约束 JSON + 代码推理 + 逻辑陷阱。"""
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
    r = client.chat([{"role": "user", "content": prompt}], max_tokens=500)
    title = "能力指纹（结构化+推理）"
    if not r["ok"]:
        return {"code": "D16", "title": title, "score": 0, "notes": [r["error"]], "raw": r}
    text = r["text"].strip()
    score = 0
    notes = []
    try:
        obj = json.loads(text)
        score += 35
        if set(obj.keys()) == {"answer", "needle", "code", "logic"}:
            score += 15
        else:
            notes.append("JSON 字段不匹配")
        if obj.get("needle") == token:
            score += 20
        else:
            notes.append("needle 不匹配")
        if "[0, 2, 4]" in str(obj.get("code")):
            score += 15
        else:
            notes.append("代码推理结果错误")
        if "热" in str(obj.get("logic")):
            score += 15
        else:
            notes.append("逻辑陷阱遗漏“热”线索")
    except Exception:  # noqa: BLE001
        notes.append("输出非合法 JSON")
        if token in text:
            score += 20
        if "[0, 2, 4]" in text:
            score += 15
        if "热" in text:
            score += 15
    return {"code": "D16", "title": title, "score": min(score, 100), "notes": notes, "raw": r}


def probe_stability(client: Client, samples: int = 5) -> dict:
    """D8/D9 性能稳定性：重复采样、失败率、一致性、正确率、延迟波动。"""
    answers = []
    times = []
    errors = []
    prompt = "计算 17*23+19。只输出最终整数。"
    for _ in range(max(1, samples)):
        r = client.chat([{"role": "user", "content": prompt}], max_tokens=30)
        if r["ok"]:
            answers.append(norm(r["text"]))
            if r["elapsed"] is not None:
                times.append(float(r["elapsed"]))
        else:
            errors.append(r["error"])
    n = max(1, samples)
    fail_rate = len(errors) / n
    consistency = max((answers.count(a) for a in set(answers)), default=0) / n
    correct_rate = sum(1 for a in answers if "410" in a) / n
    if len(times) >= 2 and statistics.mean(times) > 0:
        cv = statistics.pstdev(times) / statistics.mean(times)
    else:
        cv = 1.0
    score = 100
    score -= fail_rate * 60
    score -= max(0, 1 - consistency) * 25
    score -= max(0, 1 - correct_rate) * 20
    score -= min(cv, 1.5) * 10
    notes = [f"失败率={fail_rate:.2f}", f"一致性={consistency:.2f}", f"正确率={correct_rate:.2f}", f"延迟变异={cv:.2f}"]
    if errors:
        notes.append("错误样本=" + "; ".join(errors[:2]))
    raw = {"answers": answers, "times": times, "errors": errors}
    return {"code": "D8/D9", "title": "性能稳定性", "score": max(0, round(score, 1)), "notes": notes, "raw": raw}


def probe_stream(client: Client) -> dict:
    """S5 SSE 流式完整性。"""
    s = client.stream([{"role": "user", "content": "请用 12 个字以内回答：流式输出测试"}], max_tokens=80)
    title = "SSE 流式完整性"
    if not s["ok"]:
        return {"code": "S5", "title": title, "score": 0, "notes": [s.get("error")], "raw": s}
    score = 0
    notes = []
    if "text/event-stream" in s["content_type"]:
        score += 20
    else:
        notes.append(f"content-type 非 SSE：{s['content_type'] or '空'}")
    if s["chunks"] >= 2:
        score += 20
    else:
        notes.append("chunk 过少")
    if s["ttfb"] < 5:
        score += 20
    else:
        notes.append(f"首字节慢：{s['ttfb']:.2f}s")
    if s["done"]:
        score += 20
    else:
        notes.append("缺少结束信号")
    if s["text"].strip():
        score += 20
    else:
        notes.append("聚合文本为空")
    if s.get("error"):
        notes.append(s["error"])
    return {"code": "S5", "title": title, "score": score, "notes": notes, "raw": s}


def probe_error_leak(client: Client) -> dict:
    """S4 错误信息泄露：畸形请求是否暴露上游 URL / key / 堆栈。"""
    leaks = []
    url = client.adapter.endpoint(client.base_url)
    headers = client.adapter.headers(client.api_key)
    timeout = min(client.timeout, 30)
    for name, body in client.adapter.error_bodies(client.model):
        resp, _, error, _ = _request(url, headers, body, timeout, False)
        text = error or ""
        if resp is not None:
            text = resp.read().decode("utf-8", errors="replace")[:1000]
        if LEAK_RE.search(text):
            leaks.append((name, text[:200]))
    score = 100 if not leaks else max(0, 100 - 45 * len(leaks))
    notes = [] if not leaks else [f"{name} 可能泄露：{sample}" for name, sample in leaks]
    return {"code": "S4", "title": "错误信息泄露", "score": score, "notes": notes, "raw": {"leaks": leaks}}


PROBE_DEFS = [
    ("D1/D2/D17", 22, probe_signature),
    ("D5", 12, probe_canary),
    ("S3", 12, probe_system_lock),
    ("D16", 14, probe_capability),
    ("D8/D9", 16, probe_stability),
    ("S5", 10, probe_stream),
    ("S4", 6, probe_error_leak),
]

SCORE_BANDS = [
    (80, "未发现明显异常（不代表绝对真实）"),
    (60, "基本可用，但有短板或可疑点"),
    (40, "可疑，建议人工查看原始响应"),
    (20, "强可疑"),
    (0, "基本不可用或严重异常"),
]


def band(score: float) -> str:
    for threshold, label in SCORE_BANDS:
        if score >= threshold:
            return label
    return SCORE_BANDS[-1][1]


# --------------------------------------------------------------------------- #
# 单站审计
# --------------------------------------------------------------------------- #

def run_audit(site: dict, *, samples: int = 5, probe_codes=None, progress=None, relay_tag: str = "") -> dict:
    client = Client(site)
    results = []
    weighted = 0.0
    total_weight = 0
    caps = []
    defs = [d for d in PROBE_DEFS if (probe_codes is None or d[0] in probe_codes)]
    n = len(defs)
    for i, (code, weight, fn) in enumerate(defs):
        if progress:
            progress({"type": "probe_start", "relay": relay_tag, "code": code, "index": i, "total": n})
        res = fn(client, samples=samples) if code == "D8/D9" else fn(client)
        res["weight"] = weight
        results.append(res)
        weighted += float(res["score"]) * weight
        total_weight += weight
        if code == "D1/D2/D17" and res["score"] < 40:
            caps.append(40)
        if code == "S3" and res["score"] == 0:
            caps.append(60)
        if code == "S4" and res["score"] < 70:
            caps.append(70)
        if progress:
            progress({"type": "probe_done", "relay": relay_tag, "code": code,
                      "title": res["title"], "score": res["score"], "notes": res["notes"],
                      "index": i, "total": n})
    score = weighted / total_weight if total_weight else 0
    if caps:
        score = min(score, min(caps))
    score = round(score, 1)
    model_echo = None
    for r in results:
        raw = r.get("raw")
        if isinstance(raw, dict) and raw.get("model"):
            model_echo = raw.get("model")
            break
    return {
        "site": {k: v for k, v in site.items() if k != "api_key"},
        "score": score,
        "band": band(score),
        "caps": caps,
        "model_echo": model_echo,
        "probes": results,
    }


# --------------------------------------------------------------------------- #
# A↔B 一致性交叉校验
# --------------------------------------------------------------------------- #

CROSS_CASES = [
    ("math", "计算 17*23+19。只输出最终整数。", "410"),
    ("logic", "房间里有 3 个开关，对应隔壁房间 3 盏灯。你只能进隔壁房间一次，如何判断哪个开关对应哪盏灯？用中文简洁回答。", "热"),
    ("code", "阅读这段 Python，只给出 print 的输出，不要解释：\nitems=[]\nfor i in range(3):\n    items.append(lambda x=i: x*2)\nprint([fn() for fn in items])", "[0, 2, 4]"),
]


def cross_consistency(site_a: dict, site_b: dict, *, progress=None) -> dict:
    ca = Client(site_a)
    cb = Client(site_b)
    rows = []
    total = len(CROSS_CASES)
    for i, (cid, prompt, expect) in enumerate(CROSS_CASES):
        if progress:
            progress({"type": "cross_start", "cid": cid, "index": i, "total": total})
        ra = ca.chat([{"role": "user", "content": prompt}], max_tokens=300)
        rb = cb.chat([{"role": "user", "content": prompt}], max_tokens=300)
        ta = ra["text"] if ra["ok"] else ""
        tb = rb["text"] if rb["ok"] else ""
        agree = bool(ta) and bool(tb) and norm(ta) == norm(tb)
        sim = similarity(ta, tb)
        needle = expect.lower().replace(" ", "")
        rows.append({
            "cid": cid, "prompt": prompt, "expect": expect,
            "a_ok": ra["ok"], "b_ok": rb["ok"], "a_text": ta, "b_text": tb,
            "agree": agree, "similarity": sim,
            "a_correct": needle in norm(ta), "b_correct": needle in norm(tb),
            "a_elapsed": ra.get("elapsed"), "b_elapsed": rb.get("elapsed"),
        })
        if progress:
            progress({"type": "cross_done", "cid": cid, "agree": agree, "similarity": sim,
                      "index": i, "total": total})
    n = len(rows) or 1
    return {
        "rows": rows,
        "agreement_rate": round(sum(1 for r in rows if r["agree"]) / n, 3),
        "similarity_median": median([r["similarity"] for r in rows]),
        "a_correct_rate": round(sum(1 for r in rows if r["a_correct"]) / n, 3),
        "b_correct_rate": round(sum(1 for r in rows if r["b_correct"]) / n, 3),
        "a_latency_median": median([r["a_elapsed"] for r in rows]),
        "b_latency_median": median([r["b_elapsed"] for r in rows]),
    }


# --------------------------------------------------------------------------- #
# 对比结论
# --------------------------------------------------------------------------- #

def build_verdict(report_a: dict, report_b: dict, cross: dict) -> dict:
    sa = report_a["score"]
    sb = report_b["score"]
    na = report_a["site"].get("name", "中转 A")
    nb = report_b["site"].get("name", "中转 B")
    if abs(sa - sb) < 3:
        recommended = "基本持平"
    else:
        recommended = na if sa > sb else nb

    amap = {p["code"]: p for p in report_a["probes"]}
    bmap = {p["code"]: p for p in report_b["probes"]}
    key_diffs = []
    for code, pa in amap.items():
        pb = bmap.get(code)
        if not pb:
            continue
        delta = pa["score"] - pb["score"]
        if abs(delta) >= 15:
            key_diffs.append({"code": code, "title": pa["title"],
                              "a": pa["score"], "b": pb["score"], "delta": round(delta, 1)})
    key_diffs.sort(key=lambda x: abs(x["delta"]), reverse=True)

    flags = []
    if cross["agreement_rate"] < 0.5 and abs(sa - sb) >= 15:
        weaker = nb if sa > sb else na
        flags.append(
            f"两端对确定性题答案分歧大（一致率 {int(cross['agreement_rate'] * 100)}%），"
            f"且 {weaker} 综合明显更低，疑似一方缩水或替换模型"
        )
    ea = report_a.get("model_echo")
    eb = report_b.get("model_echo")
    if ea and eb and norm(ea) != norm(eb):
        flags.append(f"自报模型名不一致：{na} 回显 “{ea}”，{nb} 回显 “{eb}”")
    if report_a["caps"]:
        flags.append(f"{na} 触发封顶（≤{min(report_a['caps'])}）：协议/系统指令/错误泄露存在硬伤")
    if report_b["caps"]:
        flags.append(f"{nb} 触发封顶（≤{min(report_b['caps'])}）：协议/系统指令/错误泄露存在硬伤")

    return {
        "recommended": recommended,
        "score_a": sa, "score_b": sb,
        "name_a": na, "name_b": nb,
        "band_a": report_a["band"], "band_b": report_b["band"],
        "key_diffs": key_diffs,
        "flags": flags,
        "agreement_rate": cross["agreement_rate"],
    }


# --------------------------------------------------------------------------- #
# 双中转横评入口
# --------------------------------------------------------------------------- #

def compare(site_a: dict, site_b: dict, *, samples: int = 5, probe_codes=None, progress=None) -> dict:
    if progress:
        progress({"type": "stage", "stage": "A", "name": site_a.get("name", "中转 A")})
    report_a = run_audit(site_a, samples=samples, probe_codes=probe_codes, progress=progress, relay_tag="A")
    if progress:
        progress({"type": "stage", "stage": "B", "name": site_b.get("name", "中转 B")})
    report_b = run_audit(site_b, samples=samples, probe_codes=probe_codes, progress=progress, relay_tag="B")
    if progress:
        progress({"type": "stage", "stage": "cross", "name": "一致性交叉校验"})
    cross = cross_consistency(site_a, site_b, progress=progress)
    verdict = build_verdict(report_a, report_b, cross)
    result = {
        "report_a": report_a,
        "report_b": report_b,
        "cross": cross,
        "verdict": verdict,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if progress:
        progress({"type": "complete"})
    return result
