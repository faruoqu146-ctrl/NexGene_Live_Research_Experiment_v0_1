"""Pure planning helpers for NexGene's bounded live research experiment.

No network, database, credentials, or patient records are accessed in this module.
"""
import json
import re
from typing import Sequence


def clean_query_term(value: str) -> str:
    """Reduce structured hypothesis fields to a bounded scholarly search term."""
    value = re.sub(r"[^A-Za-z0-9 +._-]", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()[:80]


def plan_research_queries(hypotheses: Sequence[object], max_queries: int) -> list[dict]:
    """Create a small deterministic query plan from falsifiable hypotheses.

    This is deliberately a transparent query planner, not a claim of autonomous learning.
    A future model-based planner can replace it behind the same bounded contract.
    """
    plan = []
    seen = set()
    for h in hypotheses:
        exposure = clean_query_term(h.exposure)
        outcome = clean_query_term(h.outcome)
        if not exposure or not outcome:
            continue
        candidates = [
            ("association", f'"{exposure}" "{outcome}" longitudinal study'),
            ("replication_and_alternatives", f'"{exposure}" "{outcome}" systematic review OR meta-analysis'),
        ]
        try:
            alternatives = json.loads(h.alternative_explanations_json or "[]")
        except (TypeError, json.JSONDecodeError):
            alternatives = []
        if alternatives and len(plan) + len(candidates) < max_queries:
            alt = clean_query_term(str(alternatives[0]))
            if alt:
                candidates.append(("alternative_explanation", f'"{exposure}" "{alt}" "{outcome}"'))
        for purpose, query in candidates:
            query = query[:220]
            if query.lower() in seen:
                continue
            seen.add(query.lower())
            plan.append({"hypothesis_id": h.hypothesis_id, "purpose": purpose, "query": query})
            if len(plan) >= max_queries:
                return plan
    return plan
