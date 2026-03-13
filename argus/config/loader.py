"""Configuration loader for Argus monitoring system."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class ContactConfig(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class OwnersConfig(BaseModel):
    primary: ContactConfig
    secondary: Optional[ContactConfig] = None


class TeamsNotificationConfig(BaseModel):
    webhook_url: Optional[str] = None
    webhook_env: Optional[str] = None

    def resolve_webhook_url(self) -> Optional[str]:
        if self.webhook_env:
            return os.environ.get(self.webhook_env)
        return self.webhook_url


class SmsConfig(BaseModel):
    enabled: bool = False


class CallConfig(BaseModel):
    enabled: bool = False


class NotificationsConfig(BaseModel):
    teams: Optional[TeamsNotificationConfig] = None
    sms: Optional[SmsConfig] = None
    call: Optional[CallConfig] = None


class DependencyConfig(BaseModel):
    service: str
    region: str
    criticality: Literal["critical", "medium", "low"] = "medium"


class ActivePassiveRegions(BaseModel):
    active: str
    passive: str


class MultiRegionList(BaseModel):
    list: list[str]


class FailoverConfig(BaseModel):
    allowed: bool = False
    approval_required: bool = True
    cooldown_minutes: int = 60


class EscalationStep(BaseModel):
    delay_minutes: int = 0
    channel: Literal["teams", "sms", "call", "email"]
    target: Literal["primary", "secondary"]


class ApplicationConfig(BaseModel):
    id: str
    name: str
    team: str
    owners: OwnersConfig
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    topology: Literal["active_passive", "multi_region", "single_region"] = "single_region"
    regions: dict = Field(default_factory=dict)
    dependencies: list[DependencyConfig] = Field(default_factory=list)
    failover: Optional[FailoverConfig] = None
    escalation: list[EscalationStep] = Field(default_factory=list)

    def get_active_region(self) -> Optional[str]:
        if self.topology == "active_passive":
            return self.regions.get("active")
        return None

    def get_passive_region(self) -> Optional[str]:
        if self.topology == "active_passive":
            return self.regions.get("passive")
        return None

    def get_all_regions(self) -> list[str]:
        if self.topology == "active_passive":
            return [r for r in [self.regions.get("active"), self.regions.get("passive")] if r]
        elif self.topology == "multi_region":
            return self.regions.get("list", [])
        elif self.topology == "single_region":
            r = self.regions.get("region")
            return [r] if r else []
        return []


class GlobalConfig(BaseModel):
    polling_interval_seconds: int = 300
    ai_model: str = "gpt-4o"
    concurrency_limit: int = 5
    approval_timeout_minutes: int = 15
    log_level: str = "INFO"


class TopologyConfig(BaseModel):
    pass


class ArgusConfig(BaseModel):
    global_config: GlobalConfig = Field(alias="global", default_factory=GlobalConfig)
    applications: list[ApplicationConfig] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def load_config(config_path: str | Path = "monitor.config.yaml") -> ArgusConfig:
    """Load and validate Argus configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path.absolute()}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    return ArgusConfig.model_validate(raw)
