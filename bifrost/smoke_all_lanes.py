"""1-shot smoke test of every cloud provider/model lane through Bifrost (4445)."""
import json, time, urllib.request, sys

GATEWAY = "http://localhost:4445/v1/chat/completions"
# legion-prod VK (any project VK works — all allow all providers post-sync)
VK = "sk-bf-2aec7863-1e4f-433c-8677-6919166737d1"

MODELS = [
    "moonshot/kimi-k2.6",                                  # PARKED 2026-07-13 — compat shim -> NIM kimi-k2.6, see disabled-providers.json
    "nvidia-nim/moonshotai/kimi-k2.6",                     # KNOWN DOWN 2026-07-13 — NVIDIA 404s "Function ... Not found for account" despite being listed in /v1/models
    "nvidia-nim/z-ai/glm-5.2",                             # RENAMED 2026-07-13 (was z-ai/glm-5.1, upstream retired that id)
    "nvidia-nim/deepseek-ai/deepseek-v4-pro",
    "nvidia-nim/deepseek-ai/deepseek-v4-flash",
    "nvidia-nim/qwen/qwen3-next-80b-a3b-instruct",
    "nvidia-nim/nvidia/nemotron-3-ultra-550b-a55b",
    "openrouter/qwen/qwen3-coder:free",
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/google/gemma-4-26b-a4b-it:free",
    "openrouter/openrouter/free",
    "gemini/gemini-3-flash-preview",                       # PARKED 2026-07-08 — GEMINI_API_KEY expired, provider removed from config.json (expect 400 config-not-found)
    "gemini/gemini-3.5-flash",                             # PARKED 2026-07-08 — same as above
    "groq/openai/gpt-oss-120b",
    "cerebras/gpt-oss-120b",
    "mistral/mistral-small-latest",
    "zai/glm-4.7-flash",
    "hf-router/moonshotai/Kimi-K2.6",                      # KNOWN DOWN 2026-07-13 — HF account depleted monthly credits (402 account-wide, resets monthly)
]

for model in MODELS:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 64,
        "temperature": 1.0,
    }).encode()
    req = urllib.request.Request(
        GATEWAY, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {VK}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        ms = int((time.time() - t0) * 1000)
        msg = (data.get("choices") or [{}])[0].get("message", {})
        text = (msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or "")[:40].replace("\n", " ")
        print(f"PASS {model:55s} {ms:6d}ms  {text!r}")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode()[:160]
            except Exception:
                pass
        print(f"FAIL {model:55s} {ms:6d}ms  {e} {detail}")
    sys.stdout.flush()
