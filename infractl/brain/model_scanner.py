"""Model and Channel Discovery Scanner.
Audits configured provider models on a schedule, tests upstream channels,
prunes dead / deprecated models, and discovers newly available models.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from infractl.bifrost import admin_api
from infractl.bifrost import config as bifrost_config
from infractl.brain.client import GeminiBrainClient
from infractl.core import actions as actions_mod
from infractl.core.ledger import Ledger
from infractl.probes.lanes import load_probe_lanes, vk_allowed_providers
from infractl.settings import Settings

logger = logging.getLogger("infractl.brain.scanner")


class ModelScanner:
    def __init__(self, settings: Settings, ledger: Ledger, brain_client: GeminiBrainClient | None = None):
        self.settings = settings
        self.ledger = ledger
        self.brain = brain_client or GeminiBrainClient(settings, ledger)

    async def scan_gemini_models(self) -> dict[str, Any]:
        """Discovers available Google AI Studio models using the Gemini brain client."""
        findings: dict[str, Any] = {
            "active_models": [],
            "deprecated_models": [],
            "recommended_additions": [],
        }
        try:
            available = await self.brain.list_available_models()
            available_names = {m["name"] for m in available}
            findings["active_models"] = sorted(list(available_names))

            # Flag known obsolete models
            known_obsolete = {"gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"}
            findings["deprecated_models"] = [m for m in known_obsolete if m not in available_names]

            # Priority candidates for generation
            candidates = [
                "gemini-3.8-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemini-flash-latest",
                "gemini-pro-latest",
            ]
            findings["recommended_additions"] = [c for c in candidates if c in available_names]

        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to scan Gemini upstream models")
            findings["error"] = str(exc)

        return findings

    async def audit_active_provider_models(self, max_per_provider: int = 2) -> dict[str, Any]:
        """Audits current active provider lanes quickly using probe lanes + sample checks."""
        results: dict[str, Any] = {"healthy": [], "dead": [], "checked": 0}
        cfg_path = self.settings.config_json_path
        if not cfg_path.exists():
            return results

        probe_vk = self.settings.infra_probe_vk
        if not probe_vk:
            logger.info("Skipping live gateway model audit: INFRA_PROBE_VK unset")
            return results

        lanes, _skipped = load_probe_lanes(cfg_path, vk_allowed_providers(cfg_path.parent / "config.db", probe_vk))
        for lane in lanes:
            target = lane["model"]
            provider = target.split("/", 1)[0]
            if provider in ("vllm-local", "embed-local"):
                continue  # Local models are validated by their own dedicated probes

            results["checked"] += 1
            timeout = min(float(lane.get("timeout_s", 15.0)), 15.0)
            try:
                ok, detail = admin_api.synthetic_completion(
                    self.settings.infractl_probe_base,
                    probe_vk,
                    model=target,
                    timeout_s=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc)

            detail_lower = detail.lower()
            is_dead = (
                "model_not_found" in detail_lower
                or "404" in detail_lower
                or "410" in detail_lower
                or "no longer available" in detail_lower
                or "does not exist" in detail_lower
            )
            if is_dead:
                model_name = target.split("/", 1)[1] if "/" in target else target
                results["dead"].append({
                    "provider": provider,
                    "model": model_name,
                    "reason": detail,
                })
            elif ok:
                results["healthy"].append(target)

        return results

    async def run_discovery_and_reconcile(self, dry_run: bool | None = None) -> dict[str, Any]:
        """Runs the scheduled scan, logs findings to improvement_ledger, and prunes dead models."""
        t0 = time.time()
        gemini_findings = await self.scan_gemini_models()
        audit_findings = await self.audit_active_provider_models()

        summary = {
            "ts": time.time(),
            "duration_s": round(time.time() - t0, 2),
            "gemini_upstream": gemini_findings,
            "gateway_audit": audit_findings,
            "actions_taken": [],
        }

        # Log findings to improvement_ledger
        if audit_findings.get("dead"):
            dead_list = [f"{d['provider']}/{d['model']}" for d in audit_findings["dead"]]
            self.ledger.con.execute(
                "INSERT INTO improvement_ledger (title, detail, source, ts) VALUES (?,?,?,?)",
                (
                    f"Dead models detected ({len(dead_list)})",
                    f"Models returned 404/410/not found: {', '.join(dead_list)}",
                    "model_scanner",
                    time.time(),
                ),
            )

            # Auto-apply dead model pruning via actions.execute (with snapshot/verify/rollback)
            cfg = bifrost_config.read_config(self.settings.config_json_path)
            for dead in audit_findings["dead"]:
                p_block = cfg.get("providers", {}).get(dead["provider"], {})
                key_name = (p_block.get("keys", [{}])[0]).get("name", "primary")
                try:
                    action_res = await actions_mod.execute(
                        self.ledger,
                        self.settings,
                        "models_remove",
                        {
                            "provider": dead["provider"],
                            "key_name": key_name,
                            "models": [dead["model"]],
                        },
                        requested_by="model_scanner",
                        reason=f"Prune dead model {dead['model']} ({dead['reason'][:80]})",
                        dry_run=dry_run,
                    )
                    summary["actions_taken"].append(action_res)
                except actions_mod.ActionError as exc:
                    logger.warning("Could not auto-prune %s: %s", dead["model"], exc)

        if gemini_findings.get("recommended_additions"):
            self.ledger.con.execute(
                "INSERT INTO improvement_ledger (title, detail, source, ts) VALUES (?,?,?,?)",
                (
                    "Modern Gemini models cataloged",
                    f"Available flagships: {', '.join(gemini_findings['recommended_additions'])}",
                    "model_scanner",
                    time.time(),
                ),
            )

        return summary
