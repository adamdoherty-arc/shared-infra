"""Verification suite for the new local model through Bifrost."""
import json, time, urllib.request

GATEWAY = "http://localhost:4445/v1/chat/completions"
VK = "sk-bf-2aec7863-1e4f-433c-8677-6919166737d1"

def call(payload, timeout=180):
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {VK}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data, time.time() - t0

# 1. All three served names route correctly
for name in ["vllm-local/qwen3-chat", "vllm-local/Qwen3-32B-AWQ", "vllm-local/Qwen3.6-35B-A3B"]:
    try:
        data, dt = call({"model": name, "messages": [{"role": "user", "content": "Reply with: ok /no_think"}], "max_tokens": 16})
        msg = data["choices"][0]["message"]
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()[:20]
        print(f"PASS alias {name:35s} {int(dt*1000):5d}ms served_as={data.get('model')} -> {text!r}")
    except Exception as e:
        detail = e.read().decode()[:150] if hasattr(e, "read") else ""
        print(f"FAIL alias {name:35s} {e} {detail}")

# 2. Tool calling (the re-enabled qwen3_xml parser)
tools = [{"type": "function", "function": {
    "name": "get_stock_price",
    "description": "Get the current price for a stock ticker",
    "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}}}]
try:
    data, dt = call({"model": "vllm-local/qwen3-chat",
                     "messages": [{"role": "user", "content": "What is NVDA trading at right now? Use the tool. /no_think"}],
                     "tools": tools, "tool_choice": "auto", "max_tokens": 256})
    tc = data["choices"][0]["message"].get("tool_calls")
    if tc and tc[0]["function"]["name"] == "get_stock_price":
        args = json.loads(tc[0]["function"]["arguments"])
        print(f"PASS tools  valid tool_call get_stock_price({args}) in {int(dt*1000)}ms")
    else:
        print(f"FAIL tools  no tool_calls in response: {json.dumps(data['choices'][0]['message'])[:200]}")
except Exception as e:
    detail = e.read().decode()[:200] if hasattr(e, "read") else ""
    print(f"FAIL tools  {e} {detail}")

# 3. Long context — ~30K tokens of filler, ask for a needle
needle = "The secret launch code is PELICAN-7742."
filler = ("Market analysis paragraph about diversified portfolios and sector rotation. " * 9 + "\n")
doc = filler * 450  # ~ 30K tokens
doc = doc[: int(len(doc) * 0.5)] + "\n" + needle + "\n" + doc[int(len(doc) * 0.5):]
try:
    data, dt = call({"model": "vllm-local/qwen3-chat",
                     "messages": [{"role": "user", "content": doc + "\n\nWhat is the secret launch code? Answer with just the code. /no_think"}],
                     "max_tokens": 64}, timeout=300)
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    ptoks = data.get("usage", {}).get("prompt_tokens")
    ok = "PELICAN-7742" in text
    print(f"{'PASS' if ok else 'FAIL'} 30Kctx prompt_tokens={ptoks} answer={text[:40]!r} in {int(dt*1000)}ms")
except Exception as e:
    detail = e.read().decode()[:200] if hasattr(e, "read") else ""
    print(f"FAIL 30Kctx {e} {detail}")

# 4. Throughput — 512 completion tokens, measure decode rate
try:
    data, dt = call({"model": "vllm-local/qwen3-chat",
                     "messages": [{"role": "user", "content": "Write a 400-word essay about index funds. /no_think"}],
                     "max_tokens": 512, "temperature": 0.7}, timeout=300)
    u = data.get("usage", {})
    ctoks = u.get("completion_tokens", 0)
    print(f"PASS speed  {ctoks} tokens in {dt:.1f}s = {ctoks/dt:.0f} tok/s (old baseline ~30-50)")
except Exception as e:
    print(f"FAIL speed  {e}")

# 5. Embeddings via Bifrost
try:
    req = urllib.request.Request(
        "http://localhost:4445/v1/embeddings",
        data=json.dumps({"model": "embed-local/Qwen/Qwen3-Embedding-0.6B", "input": "hello"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {VK}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    dim = len(data["data"][0]["embedding"])
    print(f"PASS embed  dim={dim} in {int((time.time()-t0)*1000)}ms")
except Exception as e:
    detail = e.read().decode()[:150] if hasattr(e, "read") else ""
    print(f"FAIL embed  {e} {detail}")
