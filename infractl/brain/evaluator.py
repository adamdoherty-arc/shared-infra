"""Autonomous Self-Healing & Improvement Evaluator powered by Gemini.
Inspects system telemetry, probe failures, and error trends, consults the Gemini
brain for root cause analysis and remediation proposals, and records insights to the
improvement ledger.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from infractl.brain.client import BrainError, GeminiBrainClient
from infractl.core import actions as actions_mod
from infractl.core.ledger import Ledger
from infractl.settings import Settings

logger = logging.getLogger("infractl.brain.evaluator")

SYSTEM_INSTRUCTION = """You are the autonomous Infra Brain for a high-performance AI infrastructure stack.
Your stack runs local vLLM engines (RTX 5090), the Bifrost LLM gateway, Prometheus/Grafana observability,
and downstream agent systems (ADA, Legion, Zero).
Your job is to analyze system health telemetry, diagnose anomalies, recommend concrete mitigations,
and identify long-term optimizations. Keep analyses concise, actionable, and technically precise.
Format your output as JSON with keys:
{
  "summary": "Brief 1-line verdict",
  "anomaly_detected": boolean,
  "root_cause": "Explanation of any failure or degradation, or 'none'",
  "recommended_action": "T1/T2 action name or 'none'",
  "action_payload": {},
  "optimization_note": "Long-term improvement insight"
}
"""


class BrainEvaluator:
    def __init__(self, settings: Settings, ledger: Ledger, brain_client: GeminiBrainClient | None = None):
        self.settings = settings
        self.ledger = ledger
        self.brain = brain_client or GeminiBrainClient(settings, ledger)

    async def evaluate_system_health(self) -> dict[str, Any]:
        """Collects current probe states and queries the Gemini brain for diagnostic analysis."""
        probes = self.ledger.latest_probes()
        failed_probes = [p for p in probes if not p["ok"]]
        recent_actions = self.ledger.list_actions()[:5]

        telemetry = {
            "timestamp": time.time(),
            "all_probes_ok": len(failed_probes) == 0,
            "failed_probes": failed_probes,
            "healthy_probe_count": len(probes) - len(failed_probes),
            "recent_actions": [
                {"kind": a["kind"], "status": a["status"], "reason": a["reason"]}
                for a in recent_actions
            ],
            "write_mode": self.settings.infractl_write_mode,
        }

        # If everything is green and no anomalies, log a clean pulse periodically
        prompt = (
            f"Analyze current shared-infra telemetry:\n"
            f"{json.dumps(telemetry, indent=2)}\n\n"
            f"Diagnose any anomalies and provide recommendations in the requested JSON format."
        )

        try:
            raw_response = await self.brain.generate_content(
                prompt=prompt,
                system_instruction=SYSTEM_INSTRUCTION,
                purpose="self_heal_evaluation",
                temperature=0.1,
            )

            # Clean JSON markdown fences if returned
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()

            try:
                diagnosis = json.loads(cleaned)
            except json.JSONDecodeError:
                diagnosis = {
                    "summary": raw_response[:120],
                    "anomaly_detected": len(failed_probes) > 0,
                    "root_cause": raw_response,
                    "recommended_action": "none",
                    "optimization_note": "",
                }

            # Record into improvement ledger
            title = diagnosis.get("summary", "System Health Assessment")
            detail = (
                f"Root Cause: {diagnosis.get('root_cause', 'none')}\n"
                f"Action: {diagnosis.get('recommended_action', 'none')}\n"
                f"Insight: {diagnosis.get('optimization_note', '')}"
            )
            self.ledger.con.execute(
                "INSERT INTO improvement_ledger (title, detail, source, ts) VALUES (?,?,?,?)",
                (title, detail, "gemini_brain", time.time()),
            )

            # Auto-execute safe T1 actions if diagnosed with clear payload
            action_kind = diagnosis.get("recommended_action")
            action_payload = diagnosis.get("action_payload") or {}
            executed_action = None
            if action_kind and action_kind in actions_mod.REGISTRY:
                spec = actions_mod.REGISTRY[action_kind]
                if spec.tier == "T1":
                    try:
                        executed_action = await actions_mod.execute(
                            self.ledger,
                            self.settings,
                            action_kind,
                            action_payload,
                            requested_by="brain_evaluator",
                            reason=f"Brain auto-heal: {title}",
                        )
                        logger.info("Brain evaluator fired %s: %s", action_kind, executed_action)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Brain-recommended action %s could not fire: %s", action_kind, exc)

            return {
                "ok": True,
                "diagnosis": diagnosis,
                "telemetry": telemetry,
                "executed_action": executed_action,
            }

        except BrainError as exc:
            logger.warning("Gemini Brain evaluation skipped or failed: %s", exc)
            return {"ok": False, "error": str(exc), "telemetry": telemetry}
