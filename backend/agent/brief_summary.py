"""Two or three sentences from the daily brief - words only, never numbers
of the model's own.

The brief (engine.brief) is complete without this. The model is given the
brief's own lines and priority actions, asked to put them into plain words,
and its answer is kept only if every number in it is a number in the brief
(`engine.whatif.check_grounding`). Anything else - a failed call, a throttle,
an invented figure, an empty reply - returns None, and the owner sees the
deterministic lines instead. One call a day, and none when the brief has
nothing that needs attention.
"""

from __future__ import annotations

import json
from typing import Optional

from engine.whatif import check_grounding

MAX_SUMMARY_CHARS = 600

SYSTEM = (
    "You write a short morning note for the owner of a small electrical shop. "
    "Use ONLY the facts given. Do not calculate, estimate, round or add any "
    "number; copy figures exactly as written. At most three sentences, plain "
    "English, most urgent first. No greeting, no markdown.")


def summarize_brief(brief: dict, client, model_id: str) -> Optional[dict]:
    """A grounded summary, or None. Never raises."""
    if not brief.get("hasAttention"):
        return None
    facts = {"lines": brief.get("lines") or [],
             "priorityActions": [a.get("text") for a in
                                 brief.get("priorityActions") or []]}
    try:
        response = client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [
                {"text": "Facts:\n" + json.dumps(facts, ensure_ascii=False)}]}],
            inferenceConfig={"maxTokens": 220, "temperature": 0},
        )
        parts = response["output"]["message"]["content"]
        text = " ".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:  # noqa: BLE001 - the brief stands without it
        print(json.dumps({"event": "brief_summary_failed",
                          "error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
        return None
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return None
    names = [x.get("product") or "" for key in
             ("shortages", "lowStock", "supplierAlerts", "marginRisks")
             for x in brief.get(key) or []]
    grounded, unsupported = check_grounding(
        {"rows": [brief], "explanation": text}, ignore=names)
    if not grounded:
        print(json.dumps({"event": "brief_summary_rejected",
                          "ungroundedNumbers": unsupported[:10]}))
        return None
    return {"text": text, "modelId": model_id, "grounded": True}
