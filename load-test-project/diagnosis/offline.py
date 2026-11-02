"""
offline.py — the built-in explanation: plain sentences written from the computed facts, no model.
Used when the user picks "Built-in summary", and as the labelled fallback if the model fails.
"""


def summary_diagnosis(facts: dict) -> str:
    att = facts.get("attribution") or {}
    st = att.get("status")
    gp = att.get("goodput") or {}
    if st == "identified":
        s = att["suspects"][0]
        extra = ""
        if len(att["suspects"]) > 1:
            extra = f" Other flagged metrics: {', '.join(x['metric'] for x in att['suspects'][1:])}."
        return (f"Likely limiting resource: {s['metric']} ({s['resource_kind']}). {s['why']}{extra}\n"
                "Confidence: high")
    if st == "capacity_limited_unattributed":
        return (f"The service stopped scaling at about {gp.get('ceiling_rps')} successful requests/s "
                f"(from concurrency {gp.get('ceiling_from_concurrency')}), but no recorded metric explains why.\n"
                "Confidence: medium")
    if st == "no_degradation":
        return f"No degradation was found in the tested range. {att.get('reason', '')}\nConfidence: high"
    return f"The evidence does not identify a cause. {att.get('reason') or ''}\nConfidence: medium"