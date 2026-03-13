"""AWS outage correlation tool for Argus.

In production, this would query the AWS Service Health Dashboard API
or a cached copy of it. For the POC, this uses simulated data
controllable via environment variables.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel


class AwsOutageInfo(BaseModel):
    service: str
    region: str
    status: Literal["operational", "degraded", "disrupted", "unknown"]
    severity: Optional[Literal["minor", "medium", "major", "critical"]] = None
    description: Optional[str] = None
    started_at: Optional[str] = None


def get_aws_services_outage(
    services: list[str],
    regions: list[str],
) -> list[AwsOutageInfo]:
    """
    Check AWS service health for the given services and regions.

    Args:
        services: List of AWS service names (e.g., ['RDS', 'SQS', 'EC2'])
        regions: List of AWS regions (e.g., ['us-east-1', 'us-west-2'])

    Returns:
        List of AwsOutageInfo for each service/region combination.

    Override via env vars for simulation:
        ARGUS_AWS_OUTAGE_{SERVICE}_{REGION}_STATUS = operational | degraded | disrupted
    """
    results: list[AwsOutageInfo] = []

    for service in services:
        for region in regions:
            key = f"ARGUS_AWS_OUTAGE_{service.upper()}_{region.upper().replace('-', '_')}_STATUS"
            status_override = os.environ.get(key, "").lower()

            if status_override == "disrupted":
                results.append(AwsOutageInfo(
                    service=service,
                    region=region,
                    status="disrupted",
                    severity="major",
                    description=f"AWS {service} is experiencing a service disruption in {region}",
                ))
            elif status_override == "degraded":
                results.append(AwsOutageInfo(
                    service=service,
                    region=region,
                    status="degraded",
                    severity="medium",
                    description=f"AWS {service} is degraded in {region}",
                ))
            else:
                results.append(AwsOutageInfo(
                    service=service,
                    region=region,
                    status="operational",
                    severity=None,
                    description=None,
                ))

    return results
