"""LLM 客户端：走 OpenAI 兼容接口（默认用用户提供的网关）。

也提供 NullLLM 用于离线跑通流程——不联网、不花钱，先用模板顶替，
这样影子模式和回放器在没有 API 的情况下也能验证。
"""

import json
import os
import time

import requests

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


def extract_json(text):
    """从模型回复里抠出 JSON（可能被 markdown 代码块包着）。失败返回 None。

    离线知识库构建和在线情报抽取都要用：它们的提示词都要求"只输出 JSON"，
    但模型偶尔会加解释或包代码块，所以统一在这里做一次容错解析。
    """
    t = (text or "").strip()
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("{"):
                t = p
                break
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


class LLM:
    def __init__(self, cfg=None, api_key=None, base_url=None, model=None,
                 timeout=None):
        lc = (cfg or {}).get("llm", {})
        self.api_key = (api_key or lc.get("api_key")
                        or os.environ.get("DEEPSEEK_API_KEY", "")
                        or os.environ.get("GPTSAPI_KEY", ""))
        self.base_url = (base_url or lc.get("base_url")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or lc.get("model") or DEFAULT_MODEL
        self.temperature = lc.get("temperature", 0.8)
        self.max_tokens = lc.get("max_tokens", 200)
        self.timeout = timeout or lc.get("timeout", 60)
        self.thinking = lc.get("thinking") or {}
        self._thinking_ok = bool(self.thinking.get("enabled"))

    @property
    def name(self):
        extra = ""
        if self.thinking.get("enabled") and self._thinking_ok:
            extra = "+" + str(self.thinking.get("value", ""))
        return self.model + extra

    def chat(self, messages, temperature=0.8, max_tokens=None, timeout=None):
        """调用模型。

        timeout 可以按次覆盖（批量整理类调用输入长、思考久，
        用默认的 60 秒容易读超时）。
        """
        if not self.api_key:
            raise RuntimeError("缺少 API key（config.yaml 的 llm.api_key）")
        t0 = time.time()
        mt = max_tokens if max_tokens else self.max_tokens
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
        }
        # max_tokens 为 0/None 时不传该字段 —— 交给服务端用默认上限，
        # 避免思考 token 挤占输出额度导致回复被截断或为空。
        if mt:
            payload["max_tokens"] = mt
        if self.thinking.get("enabled") and self._thinking_ok:
            payload[self.thinking.get("param", "thinking_depth")] = \
                self.thinking.get("value", "low")

        r = self._post(payload, timeout)
        # 有些实现不认思考程度字段会返回 400，这时自动降级重试一次
        if r.status_code == 400 and self._thinking_ok and len(payload) > 4:
            self._thinking_ok = False
            payload.pop(self.thinking.get("param", "thinking_depth"), None)
            r = self._post(payload, timeout)
        body = r.text
        if r.status_code != 200:
            raise RuntimeError("HTTP %d: %s" % (r.status_code, body[:300]))
        data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text, int((time.time() - t0) * 1000)

    def _post(self, payload, timeout=None):
        return requests.post(
            self.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=timeout or self.timeout,
        )


class NullLLM:
    """离线占位：不调用任何模型，按简单规则拼一句话。

    目的是让影子模式和回放器在无网络/无额度时也能跑通全流程。
    """

    def __init__(self, template="收到～"):
        self.template = template

    @property
    def name(self):
        return "null-template"

    def chat(self, messages, temperature=0.8, max_tokens=200):
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content", "")
                break
        # 从上下文里捞最后一条弹幕，回一句最朴素的。
        # 行格式是 "[danmaku HH:MM] 昵称: 内容"（可能带 "  >>> " 焦点前缀）
        last = ""
        for line in user.splitlines():
            if line.lstrip("> ").startswith("[danmaku"):
                last = line
        if not last:
            draft = "收到～"
        else:
            body = last.split("] ", 1)[-1].split(": ", 1)[-1].strip()
            draft = "收到，%s" % body[:12]
        return draft, 0
