"""Notification tool for Argus.

Supports:
- Microsoft Teams / Power Automate via webhook
- SMS via Twilio
- Voice call via Twilio
"""

from __future__ import annotations

import os
from typing import Literal, Optional

import requests
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class NotificationResult(BaseModel):
    success: bool
    channel: str
    target: str
    message: str
    error: Optional[str] = None


def _build_adaptive_card(title: str, message: str, approval_url: Optional[str] = None) -> dict:
    """Build a Teams Adaptive Card payload from a title and message body.

    If approval_url is provided, an 'Approve Failover' button is appended so
    the on-call engineer can approve with a single click from Teams.
    """
    title_upper = title.upper()
    if "DOWN" in title_upper or "CRITICAL" in title_upper:
        title_color = "Attention"
    elif "DEGRADED" in title_upper or "HIGH" in title_upper:
        title_color = "Warning"
    else:
        title_color = "Good"

    lines = [l.rstrip() for l in message.splitlines()]

    def _section_lines(header: str) -> list[str]:
        try:
            start = lines.index(header) + 1
        except ValueError:
            return []
        out: list[str] = []
        for l in lines[start:]:
            if not l.strip():
                if out:
                    break
                continue
            if l.strip().isupper() and ":" not in l:
                break
            out.append(l)
        return out

    facts: list[dict] = []
    for l in lines:
        if ":" in l and l.split(":", 1)[0].strip() in {
            "Application",
            "Region",
            "Health Score",
            "Status",
            "Incident ID",
            "Detected at",
            "Approval Token",
        }:
            k, v = l.split(":", 1)
            facts.append({"title": k.strip(), "value": v.strip()})

    summary_lines = _section_lines("SUMMARY")
    outage_lines = _section_lines("AWS OUTAGE CORRELATION")
    action_lines = _section_lines("RECOMMENDED ACTIONS")

    summary_text = " ".join([s.strip() for s in summary_lines[:2] if s.strip()]).strip()
    outage_text = " ".join([s.strip() for s in outage_lines[:1] if s.strip()]).strip()

    top_actions: list[str] = []
    for l in action_lines:
        v = l.strip()
        if v:
            top_actions.append(v)
        if len(top_actions) >= 3:
            break

    body_blocks: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "weight": "Bolder",
            "size": "Large",
            "color": title_color,
            "wrap": True,
        }
    ]

    if facts:
        body_blocks.append({"type": "FactSet", "facts": facts})

    if summary_text:
        body_blocks.append({"type": "TextBlock", "text": summary_text, "wrap": True})

    if outage_text:
        body_blocks.append({"type": "TextBlock", "text": f"AWS: {outage_text}", "wrap": True, "isSubtle": True})

    if top_actions:
        body_blocks.append({"type": "TextBlock", "text": "Recommended actions", "weight": "Bolder", "wrap": True, "spacing": "Medium"})
        for a in top_actions:
            body_blocks.append({"type": "TextBlock", "text": f"• {a}", "wrap": True, "spacing": "None"})

    details_id = "argus_details"
    body_blocks.append({
        "type": "Container",
        "id": details_id,
        "isVisible": False,
        "items": [
            {
                "type": "TextBlock",
                "text": message,
                "wrap": True,
                "spacing": "Medium",
                "fontType": "Monospace",
            }
        ],
    })

    card_content: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_blocks,
    }

    actions: list[dict] = [
        {
            "type": "Action.ToggleVisibility",
            "title": "Show details",
            "targetElements": [details_id],
        }
    ]

    if approval_url:
        actions.insert(0, {
            "type": "Action.OpenUrl",
            "title": "🔐 Review & Approve Failover",
            "url": approval_url,
            "style": "positive",
        })
        actions.insert(1, {
            "type": "Action.OpenUrl",
            "title": "📋 Incident Dashboard",
            "url": approval_url.rsplit("/approve/", 1)[0] + "/",
        })

    card_content["actions"] = actions

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_content,
            }
        ],
    }


def _send_teams(
    webhook_url: str,
    message: str,
    title: str = "Argus Alert",
    approval_url: Optional[str] = None,
) -> bool:
    """Send a Teams Adaptive Card via Power Automate webhook."""
    log = logger.bind(channel="teams", webhook=webhook_url[:40] + "...")
    log.info(
        "notification_dispatching",
        strategy="teams_adaptive_card",
        title=title,
        has_approve_button=approval_url is not None,
    )

    payload = _build_adaptive_card(title, message, approval_url=approval_url)

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code in (200, 202):
            log.info("notification_delivered", strategy="teams_adaptive_card", http_status=resp.status_code)
            return True
        log.error(
            "notification_failed",
            strategy="teams_adaptive_card",
            http_status=resp.status_code,
            response_body=resp.text[:200],
        )
        return False
    except requests.RequestException as e:
        log.error("notification_request_error", strategy="teams_adaptive_card", error=str(e))
        return False


def _send_sms(phone: str, message: str) -> bool:
    """Send SMS via Twilio."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    log = logger.bind(channel="sms", to=phone)

    missing = [k for k, v in {
        "TWILIO_ACCOUNT_SID": account_sid,
        "TWILIO_AUTH_TOKEN": auth_token,
        "TWILIO_FROM_NUMBER": from_number,
    }.items() if not v]

    if missing:
        log.warning(
            "notification_dry_run",
            strategy="twilio_sms",
            reason="missing_credentials",
            missing_vars=missing,
            action="printing_to_stdout_instead",
        )
        print(f"[ARGUS SMS - DRY RUN] To: {phone}\n{message}")
        return True

    log.info(
        "notification_dispatching",
        strategy="twilio_sms",
        from_number=from_number,
        to=phone,
        message_length=len(message),
    )

    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        resp = requests.post(
            url,
            data={"From": from_number, "To": phone, "Body": message[:1600]},
            auth=(account_sid, auth_token),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info("notification_delivered", strategy="twilio_sms", http_status=resp.status_code)
            return True
        log.error(
            "notification_failed",
            strategy="twilio_sms",
            http_status=resp.status_code,
            response_body=resp.text[:200],
        )
        return False
    except requests.RequestException as e:
        log.error("notification_request_error", strategy="twilio_sms", error=str(e))
        return False


def _send_voice_call(phone: str, message: str) -> bool:
    """Initiate a voice call via Twilio TwiML."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")
    twiml_bin_url = os.environ.get("TWILIO_TWIML_BIN_URL", "")

    log = logger.bind(channel="call", to=phone)

    missing = [k for k, v in {
        "TWILIO_ACCOUNT_SID": account_sid,
        "TWILIO_AUTH_TOKEN": auth_token,
        "TWILIO_FROM_NUMBER": from_number,
    }.items() if not v]

    if missing:
        log.warning(
            "notification_dry_run",
            strategy="twilio_voice_call",
            reason="missing_credentials",
            missing_vars=missing,
            action="printing_to_stdout_instead",
        )
        print(f"[ARGUS CALL - DRY RUN] To: {phone}\n{message}")
        return True

    call_strategy = "twilio_voice_twiml_bin" if twiml_bin_url else "twilio_voice_inline_twiml"
    log.info(
        "notification_dispatching",
        strategy=call_strategy,
        from_number=from_number,
        to=phone,
        twiml_bin=bool(twiml_bin_url),
    )

    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
        if twiml_bin_url:
            call_data = {"From": from_number, "To": phone, "Url": twiml_bin_url}
        else:
            twiml = f"<Response><Say>{message[:1000]}</Say></Response>"
            call_data = {"From": from_number, "To": phone, "Twiml": twiml}

        resp = requests.post(
            url,
            data=call_data,
            auth=(account_sid, auth_token),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info("notification_delivered", strategy=call_strategy, http_status=resp.status_code)
            return True
        log.error(
            "notification_failed",
            strategy=call_strategy,
            http_status=resp.status_code,
            response_body=resp.text[:200],
        )
        return False
    except requests.RequestException as e:
        log.error("notification_request_error", strategy=call_strategy, error=str(e))
        return False


def send_notification(
    channel: Literal["teams", "sms", "call"],
    target_phone_or_webhook: str,
    message: str,
    title: str = "Argus Incident Alert",
    approval_token: Optional[str] = None,
) -> NotificationResult:
    """
    Send a notification via Teams, SMS, or voice call.

    Args:
        channel: Notification channel ('teams', 'sms', 'call')
        target_phone_or_webhook: Webhook URL for teams, phone number for sms/call
        message: The notification message body
        title: Optional title (used for Teams cards)
        approval_token: If provided and channel is 'teams', an Approve button is
            added to the card linking to ARGUS_SERVER_URL/approve/<token>.

    Returns:
        NotificationResult with success status
    """
    try:
        if channel == "teams":
            approval_url: Optional[str] = None
            if approval_token:
                server_url = os.environ.get("ARGUS_SERVER_URL", "").rstrip("/")
                if server_url:
                    approval_url = f"{server_url}/approve/{approval_token}"
                else:
                    logger.warning(
                        "approval_button_skipped",
                        reason="ARGUS_SERVER_URL not set",
                        hint="Set ARGUS_SERVER_URL=http://your-host:8080 to enable one-click approval",
                    )
            success = _send_teams(target_phone_or_webhook, message, title, approval_url=approval_url)
        elif channel == "sms":
            success = _send_sms(target_phone_or_webhook, message)
        elif channel == "call":
            success = _send_voice_call(target_phone_or_webhook, message)
        else:
            logger.error("notification_unknown_channel", channel=channel)
            return NotificationResult(
                success=False,
                channel=channel,
                target=target_phone_or_webhook,
                message=message,
                error=f"Unknown channel: {channel}",
            )

        return NotificationResult(
            success=success,
            channel=channel,
            target=target_phone_or_webhook,
            message=message,
        )
    except Exception as e:
        logger.error(
            "notification_exception",
            channel=channel,
            target=target_phone_or_webhook,
            error=str(e),
        )
        return NotificationResult(
            success=False,
            channel=channel,
            target=target_phone_or_webhook,
            message=message,
            error=str(e),
        )
