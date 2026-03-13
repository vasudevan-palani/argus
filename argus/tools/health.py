"""Health check tool for Argus.

In a real system, this would query your actual monitoring infrastructure
(CloudWatch, Datadog, New Relic, etc.). For the POC, this returns
simulated health data that can be overridden via environment config.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel


class AlarmInfo(BaseModel):
    name: str
    state: str
    severity: str = "medium"


class DependencyHealth(BaseModel):
    service: str
    region: str
    status: str
    latency_ms: Optional[float] = None


class DeploymentInfo(BaseModel):
    status: str
    version: Optional[str] = None
    deployed_at: Optional[str] = None


class HealthData(BaseModel):
    app_id: str
    region: str
    availability_percent: float
    error_rate_percent: float
    latency_p99_ms: float
    alarms: list[AlarmInfo] = []
    dependencies: list[DependencyHealth] = []
    deployment: Optional[DeploymentInfo] = None
    synthetic_test_passed: Optional[bool] = None
    raw_source: str = "simulated"


def _simulate_health(app_id: str, region: str) -> HealthData:
    """
    Simulate health data for demo purposes.
    In production, replace with real CloudWatch/Datadog/etc. API calls.

    Override via env vars:
      ARGUS_HEALTH_{APP_ID}_{REGION}_SCENARIO = healthy | degraded | down
    """
    scenario_key = f"ARGUS_HEALTH_{app_id.upper().replace('-', '_')}_{region.upper().replace('-', '_')}_SCENARIO"
    scenario = os.environ.get(scenario_key, "healthy").lower()

    if scenario == "down":
        return HealthData(
            app_id=app_id,
            region=region,
            availability_percent=0.0,
            error_rate_percent=100.0,
            latency_p99_ms=30000.0,
            alarms=[
                AlarmInfo(name="TargetResponseTime", state="ALARM", severity="critical"),
                AlarmInfo(name="HealthyHostCount", state="ALARM", severity="critical"),
                AlarmInfo(name="HTTPCode_ELB_5XX_Count", state="ALARM", severity="critical"),
            ],
            dependencies=[
                DependencyHealth(service="RDS", region=region, status="unavailable"),
            ],
            synthetic_test_passed=False,
            deployment=DeploymentInfo(status="deployed", version="1.2.3"),
            raw_source="simulated",
        )
    elif scenario == "degraded":
        return HealthData(
            app_id=app_id,
            region=region,
            availability_percent=78.5,
            error_rate_percent=12.3,
            latency_p99_ms=8500.0,
            alarms=[
                AlarmInfo(name="TargetResponseTime", state="ALARM", severity="high"),
                AlarmInfo(name="HTTPCode_ELB_5XX_Count", state="ALARM", severity="medium"),
            ],
            dependencies=[
                DependencyHealth(service="RDS", region=region, status="degraded", latency_ms=450.0),
            ],
            synthetic_test_passed=False,
            deployment=DeploymentInfo(status="deployed", version="1.2.3"),
            raw_source="simulated",
        )
    else:
        return HealthData(
            app_id=app_id,
            region=region,
            availability_percent=99.95,
            error_rate_percent=0.05,
            latency_p99_ms=250.0,
            alarms=[],
            dependencies=[
                DependencyHealth(service="RDS", region=region, status="healthy", latency_ms=12.0),
            ],
            synthetic_test_passed=True,
            deployment=DeploymentInfo(status="deployed", version="1.2.3"),
            raw_source="simulated",
        )


def get_health(app_id: str, region: str) -> HealthData:
    """
    Retrieve health data for an application in a specific region.

    Args:
        app_id: The application identifier (e.g., 'checkout-service')
        region: The AWS region (e.g., 'us-east-1')

    Returns:
        HealthData with availability, error rate, latency, alarms, etc.
    """
    return _simulate_health(app_id, region)
