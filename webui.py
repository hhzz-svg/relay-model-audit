#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webui.py — 本地双中转横评 WebUI（零第三方依赖，仅标准库）。

- 仅绑定 127.0.0.1，外部无法访问。
- API key 只在本轮请求的内存中使用，跑完即弃：不落盘、不写日志、不进 results/。
- 接口：
    GET  /                 返回单页界面
    POST /api/run          提交一次比对任务，返回 {"job_id": ...}
    GET  /api/stream?job=  SSE 实时推送探针进度与最终结果

启动：
    python webui.py                 # 默认 127.0.0.1:8731 并尝试打开浏览器
    python webui.py --port 9000
    python webui.py --no-browser
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import audit_core


ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "web" / "index.html"

# job_id -> {"queue": Queue, "result": dict|None, "error": str|None}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

MAX_BODY = 256 * 1024  # 256KB，足够两端配置


def normalize_site(raw: dict, tag: str) -> dict:
    raw = raw or {}
    return {
        "name": (raw.get("name") or f"中转 {tag}").strip(),
        "protocol": (raw.get("protocol") or "openai").strip().lower(),
        "base_url": (raw.get("base_url") or "").strip(),
        "model": (raw.get("model") or "").strip(),
        "api_key": (raw.get("api_key") or "").strip(),
        "timeout": _clamp_int(raw.get("timeout"), default=90, lo=5, hi=600),
    }


def _clamp_int(value, *, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def validate_site(site: dict, tag: str) -> str | None:
    if site["protocol"] not in audit_core.ADAPTERS:
        return f"中转 {tag} 协议无效，仅支持 openai / anthropic"
    if not site["base_url"]:
        return f"中转 {tag} 缺少 base_url"
    if not site["model"]:
        return f"中转 {tag} 缺少 model"
    if not site["api_key"]:
        return f"中转 {tag} 缺少 api_key"
    if not site["base_url"].lower().startswith(("http://", "https://")):
        return f"中转 {tag} 的 base_url 需以 http:// 或 https:// 开头"
    return None


def _worker(job_id: str, site_a: dict, site_b: dict, samples: int, probes):
    job = JOBS[job_id]
    q: queue.Queue = job["queue"]
    try:
        result = audit_core.compare(site_a, site_b, samples=samples, probe_codes=probes, progress=q.put)
        job["result"] = result
        q.put({"type": "result", "data": result})
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        q.put({"type": "error", "message": str(exc)})
    finally:
        q.put({"type": "__end__"})


class Handler(BaseHTTPRequestHandler):
    server_version = "RelayAuditWebUI/1.0"

    def log_message(self, fmt, *args):  # 静默默认日志，避免把 URL/参数打到控制台
        pass

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, content_type: str = "text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if INDEX_HTML.exists():
                self._send_text(200, INDEX_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            else:
                self._send_text(500, "web/index.html 缺失")
            return
        if path == "/api/stream":
            qs = parse_qs(urlparse(self.path).query)
            job_id = (qs.get("job") or [""])[0]
            self._stream(job_id)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._send_text(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/models"):
            self._send_text(404, "not found")
            return
        payload = self._read_json_body()
        if payload is None:
            return
        if path == "/api/models":
            self._handle_models(payload)
        else:
            self._handle_run(payload)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"error": "请求体为空或过大"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"JSON 解析失败：{exc}"})
            return None

    def _handle_models(self, payload):
        site = normalize_site(payload, "?")
        if site["protocol"] not in audit_core.ADAPTERS:
            self._send_json(400, {"error": "协议无效，仅支持 openai / anthropic"})
            return
        if not site["base_url"]:
            self._send_json(400, {"error": "缺少 base_url"})
            return
        if not site["base_url"].lower().startswith(("http://", "https://")):
            self._send_json(400, {"error": "base_url 需以 http:// 或 https:// 开头"})
            return
        if not site["api_key"]:
            self._send_json(400, {"error": "缺少 api_key"})
            return
        res = audit_core.list_models(site)
        if not res["ok"]:
            self._send_json(502, {"error": res["error"]})
            return
        self._send_json(200, {"models": res["models"]})

    def _handle_run(self, payload):
        site_a = normalize_site(payload.get("site_a"), "A")
        site_b = normalize_site(payload.get("site_b"), "B")
        samples = _clamp_int(payload.get("samples"), default=5, lo=1, hi=15)
        probes = payload.get("probes") or None
        if probes is not None:
            valid = {d[0] for d in audit_core.PROBE_DEFS}
            probes = [p for p in probes if p in valid] or None

        err = validate_site(site_a, "A") or validate_site(site_b, "B")
        if err:
            self._send_json(400, {"error": err})
            return

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"queue": queue.Queue(), "result": None, "error": None}
        threading.Thread(
            target=_worker, args=(job_id, site_a, site_b, samples, probes), daemon=True
        ).start()
        self._send_json(200, {"job_id": job_id})

    # -- SSE -------------------------------------------------------------- #
    def _stream(self, job_id: str):
        job = JOBS.get(job_id)
        if not job:
            self._send_text(404, "job not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q: queue.Queue = job["queue"]
        try:
            while True:
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    if not self._write_sse(": ping\n\n"):
                        break
                    continue
                if ev.get("type") == "__end__":
                    break
                if not self._write_sse("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"):
                    break
        finally:
            with JOBS_LOCK:
                JOBS.pop(job_id, None)

    def _write_sse(self, text: str) -> bool:
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
            return True
        except Exception:  # noqa: BLE001 — 客户端断开
            return False


def main() -> int:
    parser = argparse.ArgumentParser(description="本地双中转横评 WebUI（OpenAI / Anthropic）")
    parser.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1，仅本机访问")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"中转验货 WebUI 已启动：{url}")
    print("提示：API key 只在本机内存中使用，跑完即弃，不会写入磁盘。按 Ctrl+C 退出。")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
