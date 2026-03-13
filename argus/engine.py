"""Argus Monitoring Engine.

Core monitoring loop that:
1. Reads application configuration
2. Evaluates health for each app/region
3. Creates/updates incidents
4. Triggers AI analysis
5. Manages notification and escalation
6. Handles approval workflow for failover
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from .config.loader import ArgusConfig, ApplicationConfig
from .persistence.database import (
    Database, Incident, HealthSnapshot, ActionHistory, Approval,
    IncidentState, HealthStatus
)
from .tools.health import get_health
from .tools.aws_outage import get_aws_services_outage
from .tools.traffic import flip_application_traffic
from .agent.health_evaluator import evaluate_health
from .agent.orchestrator import AgentOrchestrator
from .notifications.escalation import EscalationManager

logger = structlog.get_logger(__name__)


class MonitoringEngine:
    """Main monitoring engine for Argus."""

    def __init__(
        self,
        config: ArgusConfig,
        db: Database,
        dry_run: bool = True,
    ):
        self.config = config
        self.db = db
        self.dry_run = dry_run
        self.orchestrator = AgentOrchestrator(model=config.global_config.ai_model)
        self.escalation_manager = EscalationManager(db)
        self._running = False
        self._escalation_threads: list[threading.Thread] = []

    def run_once(self, notification_grace_seconds: float = 0.0) -> dict:
        """Run a single monitoring cycle for all configured applications."""
        logger.info(
            "monitoring_cycle_starting",
            apps=len(self.config.applications),
            dry_run=self.dry_run,
        )

        results = {}
        for app in self.config.applications:
            try:
                results[app.id] = self._evaluate_application(app)
            except Exception as e:
                logger.error(
                    "app_evaluation_error",
                    app_id=app.id,
                    error=str(e),
                )
                results[app.id] = {"error": str(e)}

        if notification_grace_seconds > 0:
            deadline = time.time() + notification_grace_seconds
            for t in list(self._escalation_threads):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
            self._escalation_threads.clear()

        return results

    def run_continuous(self) -> None:
        """Run continuous monitoring loop."""
        self._running = True
        interval = self.config.global_config.polling_interval_seconds
        logger.info(
            "monitoring_started",
            interval_seconds=interval,
            apps=len(self.config.applications),
            dry_run=self.dry_run,
            model=self.config.global_config.ai_model,
        )

        while self._running:
            try:
                results = self.run_once()
                healthy = sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "healthy")
                degraded = sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "degraded")
                down = sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "down")
                errors = sum(1 for r in results.values() if isinstance(r, dict) and "error" in r)
                logger.info(
                    "monitoring_cycle_complete",
                    healthy=healthy,
                    degraded=degraded,
                    down=down,
                    errors=errors,
                    next_check_in_seconds=interval,
                )
            except Exception as e:
                logger.error("monitoring_cycle_error", error=str(e))

            time.sleep(interval)

    def stop(self) -> None:
        self._running = False

    def _evaluate_application(self, app: ApplicationConfig) -> dict:
        """Evaluate health for all regions of an application."""
        regions = app.get_all_regions()
        if not regions:
            logger.warning(
                "app_evaluation_skipped",
                app_id=app.id,
                reason="no_regions_configured",
            )
            return {"error": "no_regions_configured"}

        logger.info(
            "app_evaluation_starting",
            app_id=app.id,
            app_name=app.name,
            topology=app.topology,
            regions=regions,
        )

        region_results = {}
        for region in regions:
            try:
                region_results[region] = self._evaluate_region(app, region)
            except Exception as e:
                logger.error(
                    "region_evaluation_error",
                    app_id=app.id,
                    region=region,
                    error=str(e),
                )
                region_results[region] = {"error": str(e)}

        overall_status = "healthy"
        for r in region_results.values():
            if isinstance(r, dict) and r.get("health_status") == "down":
                overall_status = "down"
                break
            elif isinstance(r, dict) and r.get("health_status") == "degraded":
                overall_status = "degraded"

        logger.info(
            "app_evaluation_complete",
            app_id=app.id,
            overall_status=overall_status,
            region_statuses={r: v.get("health_status", "error") for r, v in region_results.items() if isinstance(v, dict)},
        )

        return {"status": overall_status, "regions": region_results}

    def _evaluate_region(self, app: ApplicationConfig, region: str) -> dict:
        """Evaluate health for a single application region."""
        log = logger.bind(app_id=app.id, app_name=app.name, region=region)
        log.info("region_health_check_starting")

        health_data = get_health(app.id, region)

        log.info(
            "region_health_data_retrieved",
            availability_percent=health_data.availability_percent,
            error_rate_percent=health_data.error_rate_percent,
            latency_p99_ms=health_data.latency_p99_ms,
            active_alarms=[a.name for a in health_data.alarms if a.state == "ALARM"],
            synthetic_test_passed=health_data.synthetic_test_passed,
            source=health_data.raw_source,
        )

        dep_services = list({d.service for d in app.dependencies})
        dep_regions = list({d.region for d in app.dependencies})

        log.info(
            "aws_outage_check_starting",
            checking_services=dep_services or ["EC2"],
            checking_regions=dep_regions or [region],
        )
        aws_outages = get_aws_services_outage(dep_services or ["EC2"], dep_regions or [region])

        active_outages = [o for o in aws_outages if o.status != "operational"]
        if active_outages:
            log.warning(
                "aws_outages_detected",
                count=len(active_outages),
                outages=[(o.service, o.region, o.status, o.severity) for o in active_outages],
            )
        else:
            log.info("aws_outage_check_clear", all_services_operational=True)

        evaluation = evaluate_health(health_data, aws_outages)

        log.info(
            "health_score_computed",
            score=evaluation.health_score,
            status=evaluation.health_status.value,
            breakdown=evaluation.score_breakdown,
            active_alarms=evaluation.active_alarms,
            failing_dependencies=evaluation.failing_dependencies,
        )

        snapshot = HealthSnapshot(
            app_id=app.id,
            region=region,
            health_score=evaluation.health_score,
            health_status=evaluation.health_status,
            raw_data={
                "availability": health_data.availability_percent,
                "error_rate": health_data.error_rate_percent,
                "latency_p99": health_data.latency_p99_ms,
                "alarms": [a.model_dump() for a in health_data.alarms],
            },
        )
        self.db.save_health_snapshot(snapshot)

        if evaluation.health_status == HealthStatus.HEALTHY:
            existing = self.db.get_active_incident(app.id, region)
            if existing and existing.state not in (IncidentState.RESOLVED, IncidentState.ACTION_EXECUTED):
                if existing.state == IncidentState.AWAITING_APPROVAL:
                    # Service has recovered on its own but a human hasn't acted on
                    # the failover proposal yet — leave the incident open so the
                    # approver can consciously dismiss it rather than silently losing it.
                    log.info(
                        "incident_recovery_pending_approval",
                        incident_id=existing.id[:8],
                        detail=(
                            "Service health returned to HEALTHY but a failover approval is still pending. "
                            "The incident will remain open until the approver approves or rejects it. "
                            "Visit the Argus dashboard to dismiss the proposal."
                        ),
                    )
                else:
                    existing.state = IncidentState.RESOLVED
                    existing.resolved_at = datetime.now(timezone.utc)
                    self.db.save_incident(existing)
                    log.info(
                        "incident_auto_resolved",
                        incident_id=existing.id[:8],
                        detail="Service recovered — health is now HEALTHY",
                    )
            else:
                log.info("region_healthy_no_active_incident")
            return {"health_status": "healthy", "score": evaluation.health_score}

        existing = self.db.get_active_incident(app.id, region)

        if existing:
            log.info(
                "incident_already_active",
                incident_id=existing.id[:8],
                state=existing.state.value,
                previous_score=existing.health_score,
                current_score=evaluation.health_score,
                detail="Updating score on existing incident — skipping new notifications",
            )
            existing.health_score = evaluation.health_score
            existing.health_status = evaluation.health_status
            self.db.save_incident(existing)
            return {
                "health_status": evaluation.health_status.value,
                "score": evaluation.health_score,
                "incident_id": existing.id,
                "state": existing.state.value,
            }

        log.warning(
            "incident_detected",
            status=evaluation.health_status.value,
            score=evaluation.health_score,
            alarms=evaluation.active_alarms,
            failing_deps=evaluation.failing_dependencies,
        )

        incident = Incident(
            app_id=app.id,
            app_name=app.name,
            region=region,
            health_score=evaluation.health_score,
            health_status=evaluation.health_status,
            state=IncidentState.DETECTED,
        )
        self.db.save_incident(incident)
        log.info("incident_created", incident_id=incident.id[:8])

        passive_health = None
        passive_region = app.get_passive_region()
        if passive_region and passive_region != region:
            log.info("passive_region_check_starting", passive_region=passive_region)
            try:
                passive_health_data = get_health(app.id, passive_region)
                passive_aws_outages = get_aws_services_outage(
                    dep_services or ["EC2"], [passive_region]
                )
                passive_health = evaluate_health(passive_health_data, passive_aws_outages)
                log.info(
                    "passive_region_check_complete",
                    passive_region=passive_region,
                    passive_score=passive_health.health_score,
                    passive_status=passive_health.health_status.value,
                    failover_viable=passive_health.health_status == HealthStatus.HEALTHY,
                )
            except Exception as e:
                log.warning(
                    "passive_region_check_failed",
                    passive_region=passive_region,
                    error=str(e),
                )

        failover_config = app.failover
        failover_allowed = bool(failover_config and failover_config.allowed)

        analysis = self.orchestrator.analyze_incident(
            app_id=app.id,
            app_name=app.name,
            region=region,
            evaluation=evaluation,
            health_data=health_data,
            aws_outages=aws_outages,
            passive_region_health=passive_health,
            failover_allowed=failover_allowed,
            failover_cooldown_active=False,
        )

        incident.summary = analysis.incident_summary
        incident.aws_outage_correlation = analysis.aws_outage_correlation
        incident.remediation_recommendation = "\n".join(analysis.recommended_actions)

        approval_token = None
        if (analysis.failover_recommended and
            failover_config and
            failover_config.approval_required and
            passive_region
        ):
            approval = Approval(incident_id=incident.id)
            self.db.save_approval(approval)
            approval_token = approval.token
            incident.failover_proposed = True
            incident.failover_from_region = region
            incident.failover_to_region = passive_region
            incident.state = IncidentState.AWAITING_APPROVAL
            log.warning(
                "failover_approval_pending",
                incident_id=incident.id[:8],
                from_region=region,
                to_region=passive_region,
                approval_token=approval_token,
                rationale=analysis.failover_rationale,
                detail="Run: argus approve <token> to execute the traffic flip",
            )

        self.db.save_incident(incident)

        log.info(
            "escalation_chain_dispatching",
            incident_id=incident.id[:8],
            escalation_steps=len(app.escalation),
            failover_recommended=analysis.failover_recommended,
            approval_token_issued=approval_token is not None,
        )

        escalation_thread = threading.Thread(
            target=self.escalation_manager.run_escalation_chain,
            kwargs={
                "app_config": app,
                "incident": incident,
                "analysis_summary": analysis.incident_summary,
                "aws_outage_correlation": analysis.aws_outage_correlation,
                "recommended_actions": analysis.recommended_actions,
                "failover_recommended": analysis.failover_recommended,
                "approval_token": approval_token,
            },
            daemon=True,
        )
        escalation_thread.start()
        self._escalation_threads.append(escalation_thread)

        return {
            "health_status": evaluation.health_status.value,
            "score": evaluation.health_score,
            "incident_id": incident.id,
            "state": incident.state.value,
            "failover_proposed": incident.failover_proposed,
            "approval_token": approval_token,
        }

    def process_approval(self, token: str, approved_by: str = "operator") -> dict:
        """Process a human approval for a proposed failover."""
        log = logger.bind(approved_by=approved_by)
        log.info("approval_processing", token_prefix=token[:8] + "...")

        approval = self.db.get_approval_by_token(token)
        if not approval:
            log.error("approval_rejected", reason="invalid_token")
            return {"success": False, "error": "Invalid approval token"}

        if approval.expired:
            log.error("approval_rejected", reason="token_expired", incident_id=approval.incident_id[:8])
            return {"success": False, "error": "Approval token has expired"}

        if approval.approved_at:
            log.error("approval_rejected", reason="already_approved", incident_id=approval.incident_id[:8])
            return {"success": False, "error": "Already approved"}

        incident = self.db.get_incident_by_id(approval.incident_id)
        if not incident:
            log.error("approval_rejected", reason="incident_not_found")
            return {"success": False, "error": "Incident not found"}

        if not incident.failover_from_region or not incident.failover_to_region:
            log.error("approval_rejected", reason="no_failover_target", incident_id=incident.id[:8])
            return {"success": False, "error": "No failover target configured on this incident"}

        log.info(
            "approval_accepted",
            incident_id=incident.id[:8],
            app_id=incident.app_id,
            from_region=incident.failover_from_region,
            to_region=incident.failover_to_region,
            dry_run=self.dry_run,
        )

        approval.approved_at = datetime.now(timezone.utc)
        approval.approved_by = approved_by
        self.db.save_approval(approval)

        incident.state = IncidentState.APPROVED
        self.db.save_incident(incident)

        result = flip_application_traffic(
            app_id=incident.app_id,
            from_region=incident.failover_from_region,
            to_region=incident.failover_to_region,
            approval_token=token,
            dry_run=self.dry_run,
        )

        action = ActionHistory(
            incident_id=incident.id,
            action_type="traffic_flip",
            details={
                "from_region": incident.failover_from_region,
                "to_region": incident.failover_to_region,
                "approved_by": approved_by,
                "dry_run": self.dry_run,
            },
            success=result.success,
            error=None if result.success else result.message,
        )
        self.db.save_action(action)

        if result.success:
            incident.state = IncidentState.ACTION_EXECUTED
            self.db.save_incident(incident)
            log.info(
                "traffic_flip_complete",
                incident_id=incident.id[:8],
                from_region=incident.failover_from_region,
                to_region=incident.failover_to_region,
                dry_run=self.dry_run,
                message=result.message,
            )
        else:
            log.error("traffic_flip_failed", incident_id=incident.id[:8], error=result.message)

        return {
            "success": result.success,
            "message": result.message,
            "incident_id": incident.id,
            "dry_run": self.dry_run,
        }
