"""本地假模型服务：在不花 token、不需要 key 的情况下验证 HTTP 调用链路。

它模拟 OpenAI 兼容的 /chat/completions：
- 带 response_format=json_object 的请求 -> 返回一个路由 JSON（按输入内容分支）
- 普通请求 -> 返回一段"生成"文本，回显它收到了哪些上下文段落

用法：python tools/mock_llm.py 8765
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_ROUTER = {
    "domain": "casual",
    "need_recall": False,
    "keywords": [],
    "ambiguous": False,
    "reason": "mock",
}


def route_for(user_text: str) -> dict:
    r = dict(BASE_ROUTER)
    if "论文" in user_text:
        r.update(domain="work", need_recall=False, keywords=[],
                 reason="mock: 工作任务，不需要回溯")
    elif "那件事" in user_text:
        r.update(domain="casual", need_recall=True, keywords=[],
                 ambiguous=True, reason="mock: 指代不明")
    elif "000000" in user_text:
        r.update(domain="casual", need_recall=True, keywords=["000000"],
                 reason="mock: 明确实体 + 回溯信号")
    return r


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        messages = payload.get("messages", [])
        user_last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_last = m.get("content", "")
                break
        # 路由器把"最近对话"和"当前输入"拼在一条消息里，假服务只取当前输入那半段
        if "【用户当前输入】" in user_last:
            user_last = user_last.split("【用户当前输入】", 1)[1]

        if payload.get("response_format"):
            content = json.dumps(route_for(user_last), ensure_ascii=False)
        else:
            joined = "\n".join(m.get("content", "") for m in messages)
            blocks = re.findall(r"【([^】]+)】", joined)
            content = (
                f"[mock 生成] 收到的上下文段落={blocks}｜"
                f"用户输入 {len(user_last)} 字"
            )

        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
