"""AI Agent Orchestrator for Argus.

Uses OpenAI GPT to reason over health evidence,
summarize incidents, and recommend remediation actions.

The AI is used for:
- Reasoning over health evidence
- Summarizing incidents in human-readable form
- Recommending next actions

The AI does NOT:
- Override deterministic health scores
- Execute actions without human approval
- Fabricate health data
"""

from __future__ import annotations

import json
import os
from typing import Optional

import structlog
from pydantic import BaseModel

from ..tools.health import HealthData
from ..tools.aws_outage import AwsOutageInfo
from ..agent.health_evaluator import HealthEvaluation
from ..persistence.database import HealthStatus

logger = structlog.get_logger(__name__)


class AgentAnalysis(BaseModel):
    incident_summary: str
    suspected_root_cause: str
    aws_outage_correlation: str
    recommended_actions: list[str]
    failover_recommended: bool
    failover_rationale: Optional[str] = None
    risk_level: str = "medium"
    confidence: str = "medium"


class AgentOrchestrator:
    """
    Orchestrates AI reasoning over health evaluation results.

    Uses OpenAI GPT when credentials are available.
    Falls back to deterministic reasoning when AI is unavailable.
    """

    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self._client = None
        self._ai_available = False
        self._init_ai()

    def _init_ai(self) -> None:
        """Initialize AI client if credentials are available."""
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=api_key)
                self._ai_available = True
                logger.info(
                    "analysis_mode_active",
                    mode="ai",
                    model=self.model,
                    detail=f"AI-powered incident analysis enabled using {self.model}",
                )
            except ImportError:
                logger.warning(
                    "analysis_mode_active",
                    mode="deterministic",
                    reason="openai_package_not_installed",
                    detail="Install 'openai' package to enable AI analysis",
                )
        else:
            logger.info(
                "analysis_mode_active",
                mode="deterministic",
                reason="OPENAI_API_KEY_not_set",
                detail="Set OPENAI_API_KEY to enable AI-powered analysis",
            )

    def analyze_incident(
        self,
        app_id: str,
        app_name: str,
        region: str,
        evaluation: HealthEvaluation,
        health_data: HealthData,
        aws_outages: list[AwsOutageInfo],
        passive_region_health: Optional[HealthEvaluation] = None,
        failover_allowed: bool = False,
        failover_cooldown_active: bool = False,
    ) -> AgentAnalysis:
        """
        Analyze an incident using AI reasoning (with deterministic fallback).

        Returns AgentAnalysis with summary, root cause, and recommendations.
        """
        log = logger.bind(app_id=app_id, region=region)

        if self._ai_available and self._client:
            log.info(
                "incident_analysis_start",
                mode="ai",
                model=self.model,
                health_status=evaluation.health_status.value,
                health_score=evaluation.health_score,
            )
            try:
                result = self._analyze_with_ai(
                    app_id, app_name, region, evaluation, health_data,
                    aws_outages, passive_region_health, failover_allowed, failover_cooldown_active
                )
                log.info(
                    "incident_analysis_complete",
                    mode="ai",
                    model=self.model,
                    risk_level=result.risk_level,
                    confidence=result.confidence,
                    failover_recommended=result.failover_recommended,
                )
                return result
            except Exception as e:
                log.error(
                    "incident_analysis_ai_failed",
                    error=str(e),
                    fallback="deterministic",
                    detail="Falling back to rule-based analysis",
                )

        log.info(
            "incident_analysis_start",
            mode="deterministic",
            health_status=evaluation.health_status.value,
            health_score=evaluation.health_score,
            reason="ai_unavailable_or_failed",
        )
        result = self._analyze_deterministic(
            app_id, app_name, region, evaluation, health_data,
            aws_outages, passive_region_health, failover_allowed, failover_cooldown_active
        )
        log.info(
            "incident_analysis_complete",
            mode="deterministic",
            risk_level=result.risk_level,
            confidence=result.confidence,
            failover_recommended=result.failover_recommended,
        )
        return result

    def _analyze_with_ai(
        self,
        app_id: str,
        app_name: str,
        region: str,
        evaluation: HealthEvaluation,
        health_data: HealthData,
        aws_outages: list[AwsOutageInfo],
        passive_region_health: Optional[HealthEvaluation],
        failover_allowed: bool,
        failover_cooldown_active: bool,
    ) -> AgentAnalysis:
        """Use OpenAI GPT to reason over health evidence."""
        assert self._client

        active_outages = [o for o in aws_outages if o.status != "operational"]
        log = logger.bind(app_id=app_id, region=region)

        log.debug(
            "ai_prompt_building",
            model=self.model,
            active_aws_outages=len(active_outages),
            alarms=evaluation.active_alarms,
            failing_deps=evaluation.failing_dependencies,
            failover_allowed=failover_allowed,
            passive_region=passive_region_health.region if passive_region_health else None,
            passive_status=passive_region_health.health_status.value if passive_region_health else None,
        )

        evidence = {
            "application": {"id": app_id, "name": app_name, "region": region},
            "health": {
                "score": evaluation.health_score,
                "status": evaluation.health_status.value,
                "availability_percent": health_data.availability_percent,
                "error_rate_percent": health_data.error_rate_percent,
                "latency_p99_ms": health_data.latency_p99_ms,
                "active_alarms": evaluation.active_alarms,
                "failing_dependencies": evaluation.failing_dependencies,
                "synthetic_test_passed": health_data.synthetic_test_passed,
                "score_breakdown": evaluation.score_breakdown,
            },
            "aws_outages": [o.model_dump() for o in active_outages],
            "failover": {
                "allowed": failover_allowed,
                "cooldown_active": failover_cooldown_active,
                "passive_region_health": passive_region_health.model_dump() if passive_region_health else None,
            }
        }

        system_prompt = """You are Argus, an AI platform health monitoring assistant.
Your role is to analyze operational health evidence and provide clear, actionable incident analysis.

Rules:
1. Only use the structured evidence provided — do not fabricate metrics
2. Be concise and specific
3. Recommend failover only when: active region is degraded/down AND passive is healthy AND failover is allowed AND no cooldown
4. Always explain your reasoning
5. Respond with valid JSON only

Required output format:
{
  "incident_summary": "Brief 2-3 sentence description of what is happening",
  "suspected_root_cause": "What is likely causing the issue",
  "aws_outage_correlation": "Whether AWS outages explain the issue, or 'No AWS outages detected'",
  "recommended_actions": ["Action 1", "Action 2", "Action 3"],
  "failover_recommended": true/false,
  "failover_rationale": "Why failover is/isn't recommended",
  "risk_level": "low/medium/high/critical",
  "confidence": "low/medium/high"
}"""

        user_prompt = f"Analyze this incident evidence:\n\n{json.dumps(evidence, indent=2)}"

        log.info("ai_request_sending", model=self.model, evidence_keys=list(evidence.keys()))

        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1000,
        )

        content = response.choices[0].message.content or "{}"
        data = json.loads(content)

        log.info(
            "ai_response_received",
            model=self.model,
            tokens_used=response.usage.total_tokens if response.usage else None,
            failover_recommended=data.get("failover_recommended"),
            risk_level=data.get("risk_level"),
            confidence=data.get("confidence"),
            actions_count=len(data.get("recommended_actions", [])),
        )

        return AgentAnalysis(
            incident_summary=data.get("incident_summary", evaluation.summary),
            suspected_root_cause=data.get("suspected_root_cause", "Unknown"),
            aws_outage_correlation=data.get("aws_outage_correlation", "Not analyzed"),
            recommended_actions=data.get("recommended_actions", []),
            failover_recommended=data.get("failover_recommended", False),
            failover_rationale=data.get("failover_rationale"),
            risk_level=data.get("risk_level", "medium"),
            confidence=data.get("confidence", "medium"),
        )

    def _analyze_deterministic(
        self,
        app_id: str,
        app_name: str,
        region: str,
        evaluation: HealthEvaluation,
        health_data: HealthData,
        aws_outages: list[AwsOutageInfo],
        passive_region_health: Optional[HealthEvaluation],
        failover_allowed: bool,
        failover_cooldown_active: bool,
    ) -> AgentAnalysis:
        """Deterministic fallback analysis when AI is unavailable."""
        log = logger.bind(app_id=app_id, region=region)
        active_outages = [o for o in aws_outages if o.status in ("degraded", "disrupted")]

        log.debug(
            "deterministic_analysis_inputs",
            active_aws_outages=[(o.service, o.region, o.status) for o in active_outages],
            alarms=evaluation.active_alarms,
            failing_deps=evaluation.failing_dependencies,
            failover_allowed=failover_allowed,
            failover_cooldown_active=failover_cooldown_active,
            passive_region=passive_region_health.region if passive_region_health else None,
            passive_status=passive_region_health.health_status.value if passive_region_health else None,
        )

        if active_outages:
            aws_correlation = "AWS outages detected that correlate with the application failure: " + \
                ", ".join([f"{o.service} in {o.region} ({o.status})" for o in active_outages])
        else:
            aws_correlation = "No AWS service outages detected. Failure appears to be application-level."

        actions = ["Acknowledge this incident and begin investigation"]

        if evaluation.health_status == HealthStatus.DOWN:
            actions.append(f"Immediately investigate {app_name} in {region} — service appears to be completely down")
            risk = "critical"
        else:
            actions.append(f"Investigate elevated error rates and latency in {region}")
            risk = "high"

        if evaluation.failing_dependencies:
            actions.append(f"Check failing dependencies: {', '.join(evaluation.failing_dependencies)}")

        if active_outages:
            actions.append("Monitor AWS Service Health Dashboard for outage resolution")

        failover_recommended = False
        failover_rationale = None

        passive_is_healthy = (
            passive_region_health and
            passive_region_health.health_status == HealthStatus.HEALTHY
        )
        passive_region_has_outage = any(
            o.status in ("degraded", "disrupted") for o in aws_outages
            if passive_region_health and o.region == passive_region_health.region
        )

        if (failover_allowed and
            not failover_cooldown_active and
            evaluation.health_status in (HealthStatus.DOWN, HealthStatus.DEGRADED) and
            passive_is_healthy and
            not passive_region_has_outage
        ):
            failover_recommended = True
            failover_rationale = (
                f"Active region {region} is {evaluation.health_status.value}. "
                f"Passive region {passive_region_health.region} is healthy. "
                f"Failover is recommended pending human approval."
            )
            actions.append(f"Consider approving traffic failover to {passive_region_health.region}")
            log.info(
                "deterministic_failover_recommended",
                from_region=region,
                to_region=passive_region_health.region,
                rationale=failover_rationale,
            )
        else:
            if failover_cooldown_active:
                failover_rationale = "Failover is in cooldown — a recent flip was executed."
                log.info("deterministic_failover_blocked", reason="cooldown_active")
            elif not failover_allowed:
                failover_rationale = "Failover is not configured for this application."
                log.info("deterministic_failover_blocked", reason="not_configured")
            elif not passive_is_healthy:
                passive_status = passive_region_health.health_status.value if passive_region_health else "unknown"
                failover_rationale = f"Passive region health is '{passive_status}' — failover not safe."
                log.info("deterministic_failover_blocked", reason="passive_region_unhealthy", passive_status=passive_status)
            elif passive_region_has_outage:
                failover_rationale = "Passive region is affected by AWS outages — failover not safe."
                log.info("deterministic_failover_blocked", reason="passive_region_has_aws_outage")

        summary = (
            f"{app_name} in {region} is {evaluation.health_status.value} "
            f"(score: {evaluation.health_score:.0f}/100). {evaluation.summary}."
        )

        return AgentAnalysis(
            incident_summary=summary,
            suspected_root_cause=evaluation.summary,
            aws_outage_correlation=aws_correlation,
            recommended_actions=actions,
            failover_recommended=failover_recommended,
            failover_rationale=failover_rationale,
            risk_level=risk,
            confidence="high",
        )
