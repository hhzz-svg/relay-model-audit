#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — 离线端到端自测，不需要真实 key、不产生计费。

内置一个本地假中转服务，同时支持 OpenAI(/v1/chat/completions) 与
Anthropic(/v1/messages) 两种协议，并对探针返回"能通过"的回答，
用来验证：协议解析、打分、SSE 流式、双中转横评、结果可序列化。

运行：
    python selftest.py
全部通过会打印 ALL PASS 并以退出码 0 结束。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import audit_core


SEEN_UA = []  # 记录 mock 端收到的 User-Agent，用于验证已绕开 CF 1010 的 UA 拦截


def answer(system: str, user: str) -> str:
    if "只能输出这个固定字符串" in system:
        return system.split("只能输出这个固定字符串：", 1)[1].strip()
    if "原样输出" in user:
        m = re.search(r"<([^>]+)>", user)
        if m:
            return m.group(1)
    if "JSON 对象" in user:
        m = re.search(r'needle 必须等于 "([^"]+)"', user)
        token = m.group(1) if m else "x"
        return json.dumps(
            {"answer": 17, "needle": token, "code": "[0, 2, 4]",
             "logic": "先开1号灯等几分钟后关掉，再开2号进屋：亮着的是2号，灯灭但灯泡发热的是1号，剩下的是3号。"},
            ensure_ascii=False,
        )
    if "17*23+19" in user:
        return "410"
    return "OK"


class MockRelay(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for c in chunks:
            self.wfile.write(c.encode("utf-8"))
            self.wfile.flush()

    def do_GET(self):
        SEEN_UA.append(self.headers.get("User-Agent", ""))
        if self.path.endswith("/models"):
            # 按鉴权头区分协议，顺便覆盖两种常见响应形态
            if self.headers.get("x-api-key"):
                self._json(200, {"data": [
                    {"type": "model", "id": "claude-3-5-haiku-20241022"},
                    {"type": "model", "id": "claude-3-5-sonnet-20241022"},
                ], "has_more": False})
            else:
                self._json(200, {"object": "list", "data": [
                    {"id": "gpt-4o-mini", "object": "model"},
                    {"id": "gpt-4o", "object": "model"},
                ]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        SEEN_UA.append(self.headers.get("User-Agent", ""))
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        is_anthropic = self.path.endswith("/messages")
        model = body.get("model", "")
        if is_anthropic:
            system = body.get("system", "")
            msgs = body.get("messages", [])
        else:
            system = " ".join(m.get("content", "") for m in body.get("messages", []) if m.get("role") == "system")
            msgs = [m for m in body.get("messages", []) if m.get("role") != "system"]

        # 错误用例：保持干净，不泄露任何敏感信息
        if not msgs:
            self._json(400, {"error": {"message": "messages required", "type": "invalid_request_error"}})
            return
        if model.startswith("definitely-not-a-real-model"):
            self._json(404, {"error": {"message": "model not found", "type": "not_found"}})
            return

        user = ""
        for m in msgs:
            if m.get("role") == "user":
                user = m.get("content", "")
        reply = answer(system, user)

        if body.get("stream"):
            if is_anthropic:
                self._sse([
                    'event: message_start\ndata: {"type":"message_start"}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"O"}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"K"}}\n\n',
                    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                ])
            else:
                self._sse([
                    'data: {"choices":[{"delta":{"content":"O"}}]}\n\n',
                    'data: {"choices":[{"delta":{"content":"K"}}]}\n\n',
                    'data: [DONE]\n\n',
                ])
            return

        if is_anthropic:
            self._json(200, {
                "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": max(1, len(reply))},
            })
        else:
            self._json(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": max(1, len(reply)), "total_tokens": 10 + len(reply)},
            })


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockRelay)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/v1"

    site_a = {"name": "Mock-OpenAI", "protocol": "openai", "base_url": base, "model": "mock-gpt", "api_key": "test", "timeout": 10}
    site_b = {"name": "Mock-Anthropic", "protocol": "anthropic", "base_url": base, "model": "mock-claude", "api_key": "test", "timeout": 10}

    failures = []
    def check(name, cond):
        print(("PASS" if cond else "FAIL"), "-", name)
        if not cond:
            failures.append(name)

    result = audit_core.compare(site_a, site_b, samples=3)

    check("结果含 report_a/report_b/cross/verdict",
          all(k in result for k in ("report_a", "report_b", "cross", "verdict")))
    sa = result["report_a"]["score"]
    sb = result["report_b"]["score"]
    check(f"OpenAI 端打分合理 (得分={sa})", 0 <= sa <= 100 and sa >= 80)
    check(f"Anthropic 端打分合理 (得分={sb})", 0 <= sb <= 100 and sb >= 80)

    s5_a = next(p for p in result["report_a"]["probes"] if p["code"] == "S5")
    s5_b = next(p for p in result["report_b"]["probes"] if p["code"] == "S5")
    check("OpenAI SSE 流式可解析 (S5=100)", s5_a["score"] == 100)
    check("Anthropic SSE 流式可解析 (S5=100)", s5_b["score"] == 100)

    check("两端 model 回显被正确提取",
          result["report_a"]["model_echo"] == "mock-gpt" and result["report_b"]["model_echo"] == "mock-claude")
    check("一致性交叉校验产出 3 题", len(result["cross"]["rows"]) == 3)

    try:
        json.dumps(result, ensure_ascii=False)
        serializable = True
    except Exception:
        serializable = False
    check("整个结果可 JSON 序列化（供 SSE/前端用）", serializable)

    ma = audit_core.list_models(site_a)
    mb = audit_core.list_models(site_b)
    check(f"OpenAI 端模型列表可拉取并解析 (得到 {ma['models']})",
          ma["ok"] and "gpt-4o" in ma["models"])
    check(f"Anthropic 端模型列表可拉取并解析 (得到 {mb['models']})",
          mb["ok"] and any("claude" in m for m in mb["models"]))
    check("所有出站请求都带浏览器 UA（已绕开 CF 1010 的默认 urllib UA 拦截）",
          bool(SEEN_UA) and all("Mozilla" in ua for ua in SEEN_UA))

    httpd.shutdown()
    print()
    if failures:
        print(f"{len(failures)} 项未通过：", "; ".join(failures))
        return 1
    print("ALL PASS — 双协议解析、打分、SSE、横评、序列化全链路正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
