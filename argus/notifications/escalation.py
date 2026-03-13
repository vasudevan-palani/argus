"""Escalation manager for Argus notification workflows."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from ..config.loader import ApplicationConfig, EscalationStep
from ..persistence.database import Database, Incident, NotificationAttempt, IncidentState
from ..tools.notification import send_notification

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


def _build_notification_message(
    incident: Incident,
    analysis_summary: str,
    aws_outage_correlation: str,
    recommended_actions: list[str],
    failover_recommended: bool,
    approval_token: str | None = None,
) -> str:
    """Build a clear, actionable notification message."""
    lines = [
        f"🚨 ARGUS INCIDENT ALERT",
        f"",
        f"Application: {incident.app_name}",
        f"Region: {incident.region}",
        f"Health Score: {incident.health_score:.0f}/100",
        f"Status: {incident.health_status.value.upper()}",
        f"Incident ID: {incident.id[:8]}",
        f"",
        f"SUMMARY",
        f"{analysis_summary}",
        f"",
        f"AWS OUTAGE CORRELATION",
        f"{aws_outage_correlation}",
        f"",
        f"RECOMMENDED ACTIONS",
    ]

    for i, action in enumerate(recommended_actions, 1):
        lines.append(f"{i}. {action}")

    if failover_recommended and approval_token:
        lines.extend([
            f"",
            f"⚠️  FAILOVER APPROVAL REQUIRED",
            f"Traffic failover to passive region is recommended.",
            f"Approval Token: {approval_token}",
            f"To approve: argus approve {approval_token}",
            f"This approval expires in 15 minutes.",
        ])

    lines.extend([
        f"",
        f"Detected at: {incident.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ])

    return "\n".join(lines)


class EscalationManager:
    """Manages notification escalation chains for incidents."""

    def __init__(self, db: Database):
        self.db = db

    def execute_escalation_step(
        self,
        step: EscalationStep,
        app_config: ApplicationConfig,
        incident: Incident,
        message: str,
        title: str = "Argus Incident",
        approval_token: str | None = None,
    ) -> bool:
        """Execute a single escalation step."""
        log = logger.bind(
            incident_id=incident.id[:8],
            app_id=app_config.id,
            channel=step.channel,
            target=step.target,
        )

        target_contact = (
            app_config.owners.primary if step.target == "primary"
            else app_config.owners.secondary
        )

        if not target_contact:
            log.warning(
                "escalation_step_skipped",
                reason="no_contact_configured",
                detail=f"No '{step.target}' contact defined in config for {app_config.id}",
            )
            return False

        success = False
        target_value = ""

        if step.channel == "teams":
            if not app_config.notifications.teams:
                log.warning(
                    "escalation_step_skipped",
                    reason="teams_not_configured",
                    detail="No 'notifications.teams' block in app config",
                )
            else:
                webhook = app_config.notifications.teams.resolve_webhook_url()
                if webhook:
                    log.info(
                        "escalation_step_executing",
                        strategy="power_automate_webhook",
                        contact_name=target_contact.name,
                        webhook_prefix=webhook[:40] + "...",
                    )
                    result = send_notification(
                        "teams", webhook, message, title,
                        approval_token=approval_token,
                    )
                    success = result.success
                    target_value = webhook[:40] + "..."
                else:
                    webhook_env = getattr(app_config.notifications.teams, "webhook_env", None)
                    log.warning(
                        "escalation_step_skipped",
                        reason="teams_webhook_url_missing",
                        detail=(
                            f"Env var '{webhook_env}' is not set"
                            if webhook_env else "No webhook_url or webhook_env configured"
                        ),
                    )

        elif step.channel == "sms":
            if not app_config.notifications.sms or not app_config.notifications.sms.enabled:
                log.warning(
                    "escalation_step_skipped",
                    reason="sms_not_enabled",
                    detail="Set 'notifications.sms.enabled: true' in app config to enable SMS",
                )
            elif not target_contact.phone:
                log.warning(
                    "escalation_step_skipped",
                    reason="no_phone_number",
                    detail=f"No phone number for '{step.target}' contact ({target_contact.name})",
                )
            else:
                log.info(
                    "escalation_step_executing",
                    strategy="twilio_sms",
                    contact_name=target_contact.name,
                    to=target_contact.phone,
                )
                result = send_notification("sms", target_contact.phone, message[:1600])
                success = result.success
                target_value = target_contact.phone

        elif step.channel == "call":
            if not app_config.notifications.call or not app_config.notifications.call.enabled:
                log.warning(
                    "escalation_step_skipped",
                    reason="voice_call_not_enabled",
                    detail="Set 'notifications.call.enabled: true' in app config to enable voice calls",
                )
            elif not target_contact.phone:
                log.warning(
                    "escalation_step_skipped",
                    reason="no_phone_number",
                    detail=f"No phone number for '{step.target}' contact ({target_contact.name})",
                )
            else:
                short_message = (
                    f"Argus alert. {incident.app_name} in {incident.region} "
                    f"is {incident.health_status.value}. "
                    f"Health score {incident.health_score:.0f} out of 100. "
                    f"Please check Teams for details."
                )
                log.info(
                    "escalation_step_executing",
                    strategy="twilio_voice_call",
                    contact_name=target_contact.name,
                    to=target_contact.phone,
                )
                result = send_notification("call", target_contact.phone, short_message)
                success = result.success
                target_value = target_contact.phone

        attempt = NotificationAttempt(
            incident_id=incident.id,
            channel=step.channel,
            target=step.target,
            phone_or_webhook=target_value,
            message=message[:2000],
            success=success,
        )
        self.db.save_notification(attempt)

        if success:
            log.info(
                "escalation_step_succeeded",
                channel=step.channel,
                target=step.target,
                contact_name=target_contact.name if target_contact else "unknown",
            )
        else:
            log.warning(
                "escalation_step_failed",
                channel=step.channel,
                target=step.target,
                contact_name=target_contact.name if target_contact else "unknown",
            )

        return success

    def run_escalation_chain(
        self,
        app_config: ApplicationConfig,
        incident: Incident,
        analysis_summary: str,
        aws_outage_correlation: str,
        recommended_actions: list[str],
        failover_recommended: bool = False,
        approval_token: str | None = None,
    ) -> None:
        """
        Run the full escalation chain for an incident.
        Respects delay_minutes between steps.
        """
        log = logger.bind(incident_id=incident.id[:8], app_id=app_config.id)

        if not app_config.escalation:
            log.warning(
                "escalation_skipped",
                reason="no_escalation_steps_configured",
                detail="Add an 'escalation' block to the app config to enable notifications",
            )
            return

        message = _build_notification_message(
            incident=incident,
            analysis_summary=analysis_summary,
            aws_outage_correlation=aws_outage_correlation,
            recommended_actions=recommended_actions,
            failover_recommended=failover_recommended,
            approval_token=approval_token,
        )

        title = f"Argus: {incident.app_name} {incident.health_status.value.upper()}"

        plan = [
            f"step {i+1}: {s.channel.upper()} → {s.target} (+{s.delay_minutes}m)"
            for i, s in enumerate(app_config.escalation)
        ]
        log.info(
            "escalation_chain_starting",
            total_steps=len(app_config.escalation),
            failover_recommended=failover_recommended,
            has_approval_token=approval_token is not None,
            escalation_plan=plan,
        )

        if incident.state not in (
            IncidentState.AWAITING_APPROVAL,
            IncidentState.ACKNOWLEDGED,
            IncidentState.APPROVED,
            IncidentState.ACTION_EXECUTED,
            IncidentState.RESOLVED,
        ):
            incident.state = IncidentState.NOTIFIED
            self.db.save_incident(incident)

        prev_delay = 0
        for i, step in enumerate(app_config.escalation):
            wait_seconds = (step.delay_minutes - prev_delay) * 60
            if wait_seconds > 0:
                log.info(
                    "escalation_waiting_before_next_step",
                    wait_seconds=wait_seconds,
                    wait_minutes=step.delay_minutes - prev_delay,
                    upcoming_step=f"{step.channel.upper()} → {step.target}",
                )
                time.sleep(wait_seconds)

            log.info(
                "escalation_step_starting",
                step_number=i + 1,
                total_steps=len(app_config.escalation),
                channel=step.channel,
                target=step.target,
                delay_minutes=step.delay_minutes,
            )

            self.execute_escalation_step(
                step,
                app_config,
                incident,
                message,
                title,
                approval_token=approval_token,
            )
            prev_delay = step.delay_minutes

            refreshed = self.db.get_incident_by_id(incident.id)
            if refreshed and refreshed.state in (
                IncidentState.ACKNOWLEDGED,
                IncidentState.RESOLVED,
                IncidentState.ACTION_EXECUTED,
            ):
                log.info(
                    "escalation_chain_halted",
                    reason=f"incident moved to '{refreshed.state.value}' — no further notifications needed",
                    steps_completed=i + 1,
                    steps_remaining=len(app_config.escalation) - i - 1,
                )
                return

        incident.state = IncidentState.ESCALATED
        self.db.save_incident(incident)
        log.warning(
            "escalation_chain_exhausted",
            detail="All escalation steps completed with no acknowledgement or resolution",
            total_steps=len(app_config.escalation),
        )
