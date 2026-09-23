"""infractl — shared-infra control plane.

Single writer for bifrost/config.json + config.db + the sidecars around it.
Wave 1: lock + drift guard, ledger, probes, heal rules (T1/T2 only), API,
CLI, UI. T3 actions (restart_qwen38_chat, restart_vllm_embed, vk_budget_set,
vk_rotate, compose_apply, disk_prune, model_swap) are registered but never
auto-scheduled — they execute only via POST /api/actions/{id}/approve.
"""

__version__ = "0.1.0"
