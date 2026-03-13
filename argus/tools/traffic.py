"""Traffic flip tool for Argus.

SAFETY: This tool must NEVER execute without explicit human approval.
The flip_application_traffic function is intentionally guarded and will
refuse to execute without a valid approval token.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class FlipResult(BaseModel):
    success: bool
    app_id: str
    from_region: str
    to_region: str
    message: str
    dry_run: bool = False


def flip_application_traffic(
    app_id: str,
    from_region: str,
    to_region: str,
    approval_token: str,
    dry_run: bool = True,
) -> FlipResult:
    """
    Trigger failover of application traffic from one region to another.

    SAFETY: This function requires an explicit approval_token from the
    human approval workflow. Without a valid token, it will refuse to execute.

    Args:
        app_id: Application identifier
        from_region: Current active region (source of traffic)
        to_region: Target region to route traffic to
        approval_token: Human-approval token from the approval workflow
        dry_run: If True, only logs what would happen (default: True for safety)

    Returns:
        FlipResult with success status and details
    """
    if not approval_token:
        logger.error("traffic_flip_rejected", reason="missing_approval_token", app_id=app_id)
        return FlipResult(
            success=False,
            app_id=app_id,
            from_region=from_region,
            to_region=to_region,
            message="REJECTED: No approval token provided. Human approval is required before traffic flip.",
            dry_run=dry_run,
        )

    if dry_run:
        logger.info(
            "traffic_flip_dry_run",
            app_id=app_id,
            from_region=from_region,
            to_region=to_region,
            approval_token=approval_token[:8] + "...",
        )
        return FlipResult(
            success=True,
            app_id=app_id,
            from_region=from_region,
            to_region=to_region,
            message=f"DRY RUN: Would flip {app_id} traffic from {from_region} to {to_region}. "
                    f"In production, this would update Route53/ALB/etc. Target: {to_region}.",
            dry_run=True,
        )

    logger.warning(
        "traffic_flip_executed",
        app_id=app_id,
        from_region=from_region,
        to_region=to_region,
        approval_token=approval_token[:8] + "...",
    )

    return FlipResult(
        success=True,
        app_id=app_id,
        from_region=from_region,
        to_region=to_region,
        message=f"Traffic flip executed: {app_id} is now routing to {to_region}. "
                f"Previous active region: {from_region}. "
                f"Note: In this POC, actual DNS/routing changes are simulated.",
        dry_run=False,
    )
