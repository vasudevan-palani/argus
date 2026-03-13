"""Health evaluation logic for Argus.

Combines deterministic scoring rules with AI summarization.
The AI is used for reasoning and summarization, NOT as the source of truth
for health determination. Deterministic guardrails remain in place.
"""

from __future__ import annotations

from typing import Optional

import structlog
from pydantic import BaseModel

from ..tools.health import HealthData
from ..tools.aws_outage import AwsOutageInfo
from ..persistence.database import HealthStatus

logger = structlog.get_logger(__name__)


class HealthEvaluation(BaseModel):
    app_id: str
    region: str
    health_score: float
    health_status: HealthStatus
    score_breakdown: dict[str, float]
    active_alarms: list[str]
    failing_dependencies: list[str]
    summary: str


def _score_availability(availability: float) -> float:
    """Score 0-30 based on availability percentage."""
    if availability >= 99.9:
        return 30.0
    elif availability >= 99.0:
        return 25.0
    elif availability >= 95.0:
        return 20.0
    elif availability >= 90.0:
        return 10.0
    elif availability >= 50.0:
        return 5.0
    else:
        return 0.0


def _score_error_rate(error_rate: float) -> float:
    """Score 0-25 based on error rate percentage."""
    if error_rate <= 0.1:
        return 25.0
    elif error_rate <= 1.0:
        return 20.0
    elif error_rate <= 5.0:
        return 15.0
    elif error_rate <= 20.0:
        return 5.0
    else:
        return 0.0


def _score_latency(latency_p99_ms: float) -> float:
    """Score 0-20 based on P99 latency in milliseconds."""
    if latency_p99_ms <= 500:
        return 20.0
    elif latency_p99_ms <= 1000:
        return 15.0
    elif latency_p99_ms <= 3000:
        return 10.0
    elif latency_p99_ms <= 8000:
        return 5.0
    else:
        return 0.0


def _score_alarms(health_data: HealthData) -> tuple[float, list[str]]:
    """Score 0-15 based on active alarms. Returns (score, active_alarm_names)."""
    critical_alarms = [a for a in health_data.alarms if a.state == "ALARM" and a.severity in ("critical",)]
    high_alarms = [a for a in health_data.alarms if a.state == "ALARM" and a.severity in ("high",)]
    other_alarms = [a for a in health_data.alarms if a.state == "ALARM" and a.severity not in ("critical", "high")]

    active_alarm_names = [a.name for a in health_data.alarms if a.state == "ALARM"]

    if critical_alarms:
        return 0.0, active_alarm_names
    elif high_alarms:
        return 5.0, active_alarm_names
    elif other_alarms:
        return 10.0, active_alarm_names
    else:
        return 15.0, active_alarm_names


def _score_dependencies(health_data: HealthData) -> tuple[float, list[str]]:
    """Score 0-10 based on dependency health. Returns (score, failing_deps)."""
    failing = [d for d in health_data.dependencies if d.status in ("unavailable", "degraded")]
    failing_names = [f"{d.service}:{d.region}" for d in failing]

    if not failing:
        return 10.0, []
    elif any(d.status == "unavailable" for d in failing):
        return 0.0, failing_names
    else:
        return 3.0, failing_names


def _classify_score(score: float) -> HealthStatus:
    if score >= 90:
        return HealthStatus.HEALTHY
    elif score >= 70:
        return HealthStatus.DEGRADED
    else:
        return HealthStatus.DOWN


def evaluate_health_deterministic(health_data: HealthData) -> HealthEvaluation:
    """
    Deterministic health evaluation based on operational metrics.
    This is the source of truth — AI reasoning supplements this but doesn't override it.
    """
    avail_score = _score_availability(health_data.availability_percent)
    error_score = _score_error_rate(health_data.error_rate_percent)
    latency_score = _score_latency(health_data.latency_p99_ms)
    alarm_score, active_alarms = _score_alarms(health_data)
    dep_score, failing_deps = _score_dependencies(health_data)

    total_score = avail_score + error_score + latency_score + alarm_score + dep_score

    if health_data.synthetic_test_passed is False:
        total_score = min(total_score, 60.0)

    total_score = max(0.0, min(100.0, total_score))
    health_status = _classify_score(total_score)

    score_breakdown = {
        "availability": avail_score,
        "error_rate": error_score,
        "latency": latency_score,
        "alarms": alarm_score,
        "dependencies": dep_score,
        "total": total_score,
    }

    summary_parts = []
    if health_data.availability_percent < 95:
        summary_parts.append(f"Availability critical: {health_data.availability_percent:.1f}%")
    if health_data.error_rate_percent > 5:
        summary_parts.append(f"High error rate: {health_data.error_rate_percent:.1f}%")
    if health_data.latency_p99_ms > 3000:
        summary_parts.append(f"High P99 latency: {health_data.latency_p99_ms:.0f}ms")
    if active_alarms:
        summary_parts.append(f"Active alarms: {', '.join(active_alarms)}")
    if failing_deps:
        summary_parts.append(f"Failing dependencies: {', '.join(failing_deps)}")
    if health_data.synthetic_test_passed is False:
        summary_parts.append("Synthetic tests failing")

    if not summary_parts:
        summary = f"{health_status.value.capitalize()} — all metrics within normal ranges"
    else:
        summary = "; ".join(summary_parts)

    return HealthEvaluation(
        app_id=health_data.app_id,
        region=health_data.region,
        health_score=total_score,
        health_status=health_status,
        score_breakdown=score_breakdown,
        active_alarms=active_alarms,
        failing_dependencies=failing_deps,
        summary=summary,
    )


def evaluate_health(
    health_data: HealthData,
    aws_outages: Optional[list[AwsOutageInfo]] = None,
) -> HealthEvaluation:
    """
    Evaluate health using deterministic rules.

    Args:
        health_data: Raw health signals for the application region
        aws_outages: Optional AWS outage data for correlation

    Returns:
        HealthEvaluation with score, status, and summary
    """
    evaluation = evaluate_health_deterministic(health_data)

    if aws_outages:
        active_outages = [o for o in aws_outages if o.status in ("degraded", "disrupted")]
        if active_outages and evaluation.health_status != HealthStatus.HEALTHY:
            outage_desc = "; ".join([
                f"{o.service} ({o.region}): {o.status}" for o in active_outages
            ])
            evaluation.summary += f". AWS outages detected that may explain failures: {outage_desc}"

    logger.info(
        "health_evaluated",
        app_id=evaluation.app_id,
        region=evaluation.region,
        score=evaluation.health_score,
        status=evaluation.health_status.value,
    )

    return evaluation
