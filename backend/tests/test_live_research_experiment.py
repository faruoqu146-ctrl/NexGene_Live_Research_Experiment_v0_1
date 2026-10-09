from types import SimpleNamespace

from backend.app.live_research import clean_query_term, plan_research_queries

LIVE_RESEARCH_EXPERIMENT_VERSION = "lre-0.1"


def hypothesis(hypothesis_id, exposure, outcome, alternatives=None):
    import json
    return SimpleNamespace(
        hypothesis_id=hypothesis_id,
        exposure=exposure,
        outcome=outcome,
        alternative_explanations_json=json.dumps(alternatives or []),
        status="TESTABLE",
    )


def test_query_cleaner_removes_query_control_characters_and_bounds_length():
    cleaned = clean_query_term('sleep\n" OR *: cancer')
    assert "\n" not in cleaned
    assert len(cleaned) <= 80
    assert "*" not in cleaned


def test_planner_creates_bounded_queries_from_structured_hypotheses():
    hs = [hypothesis("h1", "sleep duration", "next day energy", ["stress"])]
    plan = plan_research_queries(hs, max_queries=3)
    assert len(plan) == 3
    assert {item["hypothesis_id"] for item in plan} == {"h1"}
    assert {item["purpose"] for item in plan} == {
        "association", "replication_and_alternatives", "alternative_explanation"
    }
    assert all(len(item["query"]) <= 220 for item in plan)


def test_planner_deduplicates_and_respects_query_cap():
    hs = [
        hypothesis("h1", "sleep", "energy"),
        hypothesis("h2", "sleep", "energy"),
        hypothesis("h3", "movement", "stress"),
    ]
    plan = plan_research_queries(hs, max_queries=2)
    assert len(plan) == 2
    assert len({item["query"].lower() for item in plan}) == len(plan)


def test_experiment_has_explicit_non_llm_version():
    assert LIVE_RESEARCH_EXPERIMENT_VERSION == "lre-0.1"


def test_api_contract_keeps_research_bounded_and_user_scoped():
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '@app.post("/api/v1/intelligence/research/run")' in main
    assert 'require_csrf(request, st)' in main
    assert 'rate_limit(request, "live_research", 3, s)' in main
    assert 'EvidenceSnapshot.user_id == u.id' in main
    assert 'model_interpretation":"not_performed"' in main
