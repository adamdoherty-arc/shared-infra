"""qwen3-rerank service (2026-09-25) -- fixture verification + payload unit test.

`qwen3-rerank` is the CPU reranker sidecar added alongside `vllm-embed`
(docker-compose.vllm.yml, `ghcr.io/ggml-org/llama.cpp:server`, official
`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` -- community requantizations of
this model strip the classifier head and return near-zero scores for every
document regardless of query; only the ggml-org build is correct). This
module has two tests:

- `test_build_rerank_payload` -- pure unit test, no network, of the request
  payload builder below.
- `test_live_relevant_docs_outrank_irrelevant` -- `@pytest.mark.live`,
  requires the qwen3-rerank container reachable at QWEN3_RERANK_URL
  (default http://127.0.0.1:8002). Runs the same 3-query / 10-document
  relevance fixture used to validate the service before it shipped and
  asserts every clearly-relevant document outscores every clearly-irrelevant
  one for its query. Not run in the default gate (`-m "not live"`).
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

_RERANK_URL = os.getenv("QWEN3_RERANK_URL", "http://127.0.0.1:8002") + "/v1/rerank"
_REQUEST_TIMEOUT_S = 30

FIXTURE = {
    "capital of France": {
        "relevant": ["Paris is the capital and largest city of France, located on the Seine river."],
        "irrelevant": [
            "Bananas are a good source of potassium and dietary fiber.",
            "The Great Barrier Reef is located off the coast of Queensland, Australia.",
            "Python's asyncio module implements single-threaded concurrency.",
        ],
    },
    "how does photosynthesis work": {
        "relevant": [
            "Photosynthesis is the process by which plants convert light energy, water, "
            "and carbon dioxide into glucose and oxygen."
        ],
        "irrelevant": [
            "The stock market closed higher today after a rally in tech shares.",
            "Basketball is a team sport played on a rectangular court.",
            "Reranker models score query-document pairs for relevance.",
        ],
    },
    "symptoms of the common cold": {
        "relevant": ["Common cold symptoms include a runny nose, sore throat, cough, and mild fever."],
        "irrelevant": [
            "Mount Everest is the tallest mountain above sea level on Earth.",
            "Docker Compose orchestrates multi-container applications.",
        ],
    },
}


def build_rerank_payload(model: str, query: str, documents: list[str], top_n: int | None = None) -> dict:
    """Build the `/v1/rerank` request body llama.cpp's reranking endpoint expects.

    Required fields are `model`, `query`, `documents`; `top_n` is optional
    and omitted entirely when not given (llama.cpp treats an explicit
    `top_n: null` differently from an absent key on some builds, so this
    builder never emits the key unset).
    """
    if not query:
        raise ValueError("query must be non-empty")
    if not documents:
        raise ValueError("documents must be non-empty")
    payload: dict = {"model": model, "query": query, "documents": list(documents)}
    if top_n is not None:
        payload["top_n"] = top_n
    return payload


def test_build_rerank_payload():
    payload = build_rerank_payload("qwen3-reranker-0.6b", "capital of France", ["a", "b"])
    assert payload == {
        "model": "qwen3-reranker-0.6b",
        "query": "capital of France",
        "documents": ["a", "b"],
    }
    assert "top_n" not in payload

    with_top_n = build_rerank_payload("qwen3-reranker-0.6b", "q", ["a", "b", "c"], top_n=2)
    assert with_top_n["top_n"] == 2

    with pytest.raises(ValueError):
        build_rerank_payload("qwen3-reranker-0.6b", "", ["a"])
    with pytest.raises(ValueError):
        build_rerank_payload("qwen3-reranker-0.6b", "q", [])


def _call_rerank(query: str, documents: list[str]) -> list[dict]:
    body = json.dumps(build_rerank_payload("qwen3-reranker-0.6b", query, documents)).encode()
    req = urllib.request.Request(_RERANK_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read())["results"]


@pytest.mark.live
def test_live_relevant_docs_outrank_irrelevant():
    try:
        urllib.request.urlopen(_RERANK_URL.replace("/v1/rerank", "/health"), timeout=5)
    except OSError:
        pytest.skip(f"qwen3-rerank not reachable at {_RERANK_URL}")

    for query, groups in FIXTURE.items():
        documents = groups["relevant"] + groups["irrelevant"]
        n_relevant = len(groups["relevant"])
        results = _call_rerank(query, documents)
        scores = {r["index"]: r["relevance_score"] for r in results}
        relevant_scores = [scores[i] for i in range(n_relevant)]
        irrelevant_scores = [scores[i] for i in range(n_relevant, len(documents))]
        assert min(relevant_scores) > max(irrelevant_scores), (
            f"query={query!r}: relevant={relevant_scores} irrelevant={irrelevant_scores}"
        )
