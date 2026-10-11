"""Agent debate engine for candidate trade plans.

This module runs the committee. It does not execute trades. The orchestrator
can use its output as a final gate before the existing validator/risk/trader
path.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import (
    execution_quality_agent,
    fundamentals_analyst,
    market_regime_agent,
    news_impact_agent,
    sector_rotation_agent,
)
from agents.agent_memory import log_debate, log_decision


BLOCKING_RECOMMENDATIONS = {"BLOCK_TRADE"}
SUPPORTING_RECOMMENDATIONS = {"SUPPORT_TRADE", "BOOST_TRADE", "PASS"}
REDUCING_RECOMMENDATIONS = {"REDUCE_CONFIDENCE", "MIXED"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def _base_score(plan: dict[str, Any]) -> float:
    return _safe_float(
        plan.get("weighted_score")
        or plan.get("confidence_score")
        or (_safe_float(plan.get("confidence")) * 100),
        0.0,
    )


def _moderate(plan: dict[str, Any], votes: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = str(plan.get("symbol", "")).upper()
    hard_vetoes = [vote for vote in votes if vote.get("hard_veto")]
    score = _base_score(plan) + sum(_safe_float(vote.get("score_delta")) for vote in votes)
    score = round(max(0.0, min(100.0, score)), 2)

    blockers = [
        vote for vote in votes
        if str(vote.get("recommendation", "")).upper() in BLOCKING_RECOMMENDATIONS
    ]
    supporters = [
        vote for vote in votes
        if str(vote.get("recommendation", "")).upper() in SUPPORTING_RECOMMENDATIONS
    ]
    reducers = [
        vote for vote in votes
        if str(vote.get("recommendation", "")).upper() in REDUCING_RECOMMENDATIONS
    ]

    if hard_vetoes or blockers:
        approved = False
        final_action = "REJECT"
        final_reason = "Trade blocked by " + ", ".join(
            vote["agent_name"] for vote in hard_vetoes or blockers
        )
    elif score < 50:
        approved = False
        final_action = "REJECT"
        final_reason = f"Debate confidence too low after agent adjustments ({score:.1f})."
    elif len(reducers) >= 3 and len(supporters) <= 1:
        approved = False
        final_action = "REJECT"
        final_reason = "Too many agents reduced confidence without enough support."
    else:
        approved = True
        final_action = str(plan.get("direction", "HOLD")).upper()
        final_reason = (
            f"Approved by debate: {len(supporters)} supportive votes, "
            f"{len(reducers)} caution votes, final confidence {score:.1f}."
        )

    return {
        "symbol": symbol,
        "approved": approved,
        "final_action": final_action,
        "final_confidence": score,
        "final_reason": final_reason,
        "supporters": len(supporters),
        "cautions": len(reducers),
        "blockers": len(blockers),
    }


def debate_plan(plan: dict[str, Any], persist: bool = True) -> dict[str, Any]:
    """Run all debate agents for one trade plan."""
    run_id = str(uuid.uuid4())
    votes = [
        news_impact_agent.analyze(plan, run_id=run_id),
        fundamentals_analyst.analyze(plan, run_id=run_id),
        market_regime_agent.analyze(plan, run_id=run_id),
        sector_rotation_agent.analyze(plan, run_id=run_id),
        execution_quality_agent.analyze(plan, run_id=run_id),
    ]
    moderation = _moderate(plan, votes)
    debated_plan = {
        **plan,
        "debate_run_id": run_id,
        "debate_approved": moderation["approved"],
        "debate_final_action": moderation["final_action"],
        "debate_confidence": moderation["final_confidence"],
        "debate_reason": moderation["final_reason"],
        "debate_votes": votes,
        "weighted_score": moderation["final_confidence"],
        "confidence_score": moderation["final_confidence"],
    }
    if moderation["approved"]:
        debated_plan["direction"] = moderation["final_action"]

    if persist:
        for vote in votes:
            log_decision(vote)
        log_debate(
            run_id,
            str(plan.get("symbol", "")).upper(),
            plan,
            votes,
            moderation["final_action"],
            moderation["approved"],
            moderation["final_confidence"],
            moderation["final_reason"],
        )
    return debated_plan


def debate_plans(plans: list[dict[str, Any]], persist: bool = True) -> list[dict[str, Any]]:
    """Debate and return only approved plans, ranked by debate confidence."""
    debated = [debate_plan(plan, persist=persist) for plan in plans]
    approved = [plan for plan in debated if plan.get("debate_approved")]
    approved.sort(key=lambda item: _safe_float(item.get("debate_confidence")), reverse=True)
    return approved


if __name__ == "__main__":
    sample = {
        "symbol": "RELIANCE",
        "direction": "BUY",
        "weighted_score": 60,
        "entry_price": 100,
        "target_price": 102,
        "stop_loss_price": 99,
    }
    print(json.dumps(debate_plan(sample, persist=False), indent=2, default=str))
