"""SQLite persistence layer for Argus."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class IncidentState(str, Enum):
    DETECTED = "detected"
    NOTIFIED = "notified"
    ACKNOWLEDGED = "acknowledged"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    ACTION_EXECUTED = "action_executed"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    app_id: str
    app_name: str
    region: str
    health_score: float
    health_status: HealthStatus
    state: IncidentState = IncidentState.DETECTED
    summary: str = ""
    aws_outage_correlation: Optional[str] = None
    remediation_recommendation: Optional[str] = None
    failover_proposed: bool = False
    failover_from_region: Optional[str] = None
    failover_to_region: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None


class NotificationAttempt(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    channel: str
    target: str
    phone_or_webhook: Optional[str] = None
    message: str
    success: bool = False
    error: Optional[str] = None
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    expired: bool = False
    token: str = Field(default_factory=lambda: str(uuid.uuid4()))


class ActionHistory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    action_type: str
    details: dict = Field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HealthSnapshot(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    app_id: str
    region: str
    health_score: float
    health_status: HealthStatus
    raw_data: dict = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Database:
    """SQLite database for Argus persistence."""

    def __init__(self, db_path: str | Path = "argus.db"):
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self) -> None:
        assert self._conn
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                app_name TEXT NOT NULL,
                region TEXT NOT NULL,
                health_score REAL NOT NULL,
                health_status TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'detected',
                summary TEXT DEFAULT '',
                aws_outage_correlation TEXT,
                remediation_recommendation TEXT,
                failover_proposed INTEGER DEFAULT 0,
                failover_from_region TEXT,
                failover_to_region TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS notification_attempts (
                id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                target TEXT NOT NULL,
                phone_or_webhook TEXT,
                message TEXT NOT NULL,
                success INTEGER DEFAULT 0,
                error TEXT,
                attempted_at TEXT NOT NULL,
                FOREIGN KEY (incident_id) REFERENCES incidents(id)
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT,
                expired INTEGER DEFAULT 0,
                token TEXT NOT NULL UNIQUE,
                FOREIGN KEY (incident_id) REFERENCES incidents(id)
            );

            CREATE TABLE IF NOT EXISTS action_history (
                id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                success INTEGER DEFAULT 0,
                error TEXT,
                executed_at TEXT NOT NULL,
                FOREIGN KEY (incident_id) REFERENCES incidents(id)
            );

            CREATE TABLE IF NOT EXISTS health_snapshots (
                id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                region TEXT NOT NULL,
                health_score REAL NOT NULL,
                health_status TEXT NOT NULL,
                raw_data TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_app_id ON incidents(app_id);
            CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state);
            CREATE INDEX IF NOT EXISTS idx_health_snapshots_app_region ON health_snapshots(app_id, region);
        """)
        self._conn.commit()

    def save_incident(self, incident: Incident) -> None:
        assert self._conn
        incident.updated_at = datetime.now(timezone.utc)
        self._conn.execute("""
            INSERT INTO incidents (id, app_id, app_name, region, health_score, health_status, state,
                summary, aws_outage_correlation, remediation_recommendation, failover_proposed,
                failover_from_region, failover_to_region, created_at, updated_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                health_score=excluded.health_score,
                health_status=excluded.health_status,
                state=excluded.state,
                summary=excluded.summary,
                aws_outage_correlation=excluded.aws_outage_correlation,
                remediation_recommendation=excluded.remediation_recommendation,
                failover_proposed=excluded.failover_proposed,
                failover_from_region=excluded.failover_from_region,
                failover_to_region=excluded.failover_to_region,
                updated_at=excluded.updated_at,
                resolved_at=excluded.resolved_at
        """, (
            incident.id, incident.app_id, incident.app_name, incident.region,
            incident.health_score, incident.health_status.value, incident.state.value,
            incident.summary, incident.aws_outage_correlation, incident.remediation_recommendation,
            int(incident.failover_proposed), incident.failover_from_region, incident.failover_to_region,
            incident.created_at.isoformat(), incident.updated_at.isoformat(),
            incident.resolved_at.isoformat() if incident.resolved_at else None
        ))
        self._conn.commit()

    def get_active_incident(self, app_id: str, region: str) -> Optional[Incident]:
        assert self._conn
        terminal_states = (IncidentState.RESOLVED.value, IncidentState.ACTION_EXECUTED.value)
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE app_id=? AND region=? AND state NOT IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            (app_id, region, *terminal_states)
        ).fetchone()
        return self._row_to_incident(dict(row)) if row else None

    def get_incident_by_id(self, incident_id: str) -> Optional[Incident]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        return self._row_to_incident(dict(row)) if row else None

    def list_incidents(self, app_id: Optional[str] = None, limit: int = 50) -> list[Incident]:
        assert self._conn
        if app_id:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE app_id=? ORDER BY created_at DESC LIMIT ?",
                (app_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_incident(dict(r)) for r in rows]

    def count_incidents(self, app_id: Optional[str] = None) -> int:
        assert self._conn
        if app_id:
            row = self._conn.execute(
                "SELECT COUNT(1) AS c FROM incidents WHERE app_id=?",
                (app_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(1) AS c FROM incidents").fetchone()
        return int(row["c"]) if row else 0

    def list_incidents_paginated(self, app_id: Optional[str] = None, limit: int = 20, offset: int = 0) -> list[Incident]:
        assert self._conn
        if app_id:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE app_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (app_id, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_incident(dict(r)) for r in rows]

    def _row_to_incident(self, row: dict) -> Incident:
        return Incident(
            id=row["id"],
            app_id=row["app_id"],
            app_name=row["app_name"],
            region=row["region"],
            health_score=row["health_score"],
            health_status=HealthStatus(row["health_status"]),
            state=IncidentState(row["state"]),
            summary=row["summary"] or "",
            aws_outage_correlation=row["aws_outage_correlation"],
            remediation_recommendation=row["remediation_recommendation"],
            failover_proposed=bool(row["failover_proposed"]),
            failover_from_region=row["failover_from_region"],
            failover_to_region=row["failover_to_region"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
        )

    def save_notification(self, notif: NotificationAttempt) -> None:
        assert self._conn
        self._conn.execute("""
            INSERT OR REPLACE INTO notification_attempts
            (id, incident_id, channel, target, phone_or_webhook, message, success, error, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            notif.id, notif.incident_id, notif.channel, notif.target,
            notif.phone_or_webhook, notif.message, int(notif.success),
            notif.error, notif.attempted_at.isoformat()
        ))
        self._conn.commit()

    def _row_to_notification_attempt(self, row: dict) -> NotificationAttempt:
        return NotificationAttempt(
            id=row["id"],
            incident_id=row["incident_id"],
            channel=row["channel"],
            target=row["target"],
            phone_or_webhook=row.get("phone_or_webhook"),
            message=row.get("message") or "",
            success=bool(row.get("success")),
            error=row.get("error"),
            attempted_at=datetime.fromisoformat(row["attempted_at"]),
        )

    def count_notification_attempts(self, app_id: Optional[str] = None, incident_id: Optional[str] = None) -> int:
        assert self._conn
        if incident_id:
            row = self._conn.execute(
                "SELECT COUNT(1) AS c FROM notification_attempts WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            return int(row["c"]) if row else 0

        if app_id:
            row = self._conn.execute(
                """
                SELECT COUNT(1) AS c
                FROM notification_attempts na
                JOIN incidents i ON i.id = na.incident_id
                WHERE i.app_id=?
                """,
                (app_id,),
            ).fetchone()
            return int(row["c"]) if row else 0

        row = self._conn.execute("SELECT COUNT(1) AS c FROM notification_attempts").fetchone()
        return int(row["c"]) if row else 0

    def list_notification_attempts_paginated(
        self,
        app_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[NotificationAttempt]:
        assert self._conn
        if incident_id:
            rows = self._conn.execute(
                "SELECT * FROM notification_attempts WHERE incident_id=? ORDER BY attempted_at DESC LIMIT ? OFFSET ?",
                (incident_id, limit, offset),
            ).fetchall()
            return [self._row_to_notification_attempt(dict(r)) for r in rows]

        if app_id:
            rows = self._conn.execute(
                """
                SELECT na.*
                FROM notification_attempts na
                JOIN incidents i ON i.id = na.incident_id
                WHERE i.app_id=?
                ORDER BY na.attempted_at DESC
                LIMIT ? OFFSET ?
                """,
                (app_id, limit, offset),
            ).fetchall()
            return [self._row_to_notification_attempt(dict(r)) for r in rows]

        rows = self._conn.execute(
            "SELECT * FROM notification_attempts ORDER BY attempted_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_notification_attempt(dict(r)) for r in rows]

    def save_approval(self, approval: Approval) -> None:
        assert self._conn
        self._conn.execute("""
            INSERT OR REPLACE INTO approvals
            (id, incident_id, requested_at, approved_at, approved_by, expired, token)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            approval.id, approval.incident_id,
            approval.requested_at.isoformat(),
            approval.approved_at.isoformat() if approval.approved_at else None,
            approval.approved_by, int(approval.expired), approval.token
        ))
        self._conn.commit()

    def get_approval_by_token(self, token: str) -> Optional[Approval]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE token=?", (token,)
        ).fetchone()
        if row:
            row = dict(row)
            return Approval(
                id=row["id"],
                incident_id=row["incident_id"],
                requested_at=datetime.fromisoformat(row["requested_at"]),
                approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
                approved_by=row["approved_by"],
                expired=bool(row["expired"]),
                token=row["token"],
            )
        return None

    def get_approval_for_incident(self, incident_id: str) -> Optional[Approval]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE incident_id=? ORDER BY requested_at DESC LIMIT 1",
            (incident_id,)
        ).fetchone()
        if row:
            row = dict(row)
            return Approval(
                id=row["id"],
                incident_id=row["incident_id"],
                requested_at=datetime.fromisoformat(row["requested_at"]),
                approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
                approved_by=row["approved_by"],
                expired=bool(row["expired"]),
                token=row["token"],
            )
        return None

    def save_action(self, action: ActionHistory) -> None:
        assert self._conn
        self._conn.execute("""
            INSERT OR REPLACE INTO action_history
            (id, incident_id, action_type, details, success, error, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            action.id, action.incident_id, action.action_type,
            json.dumps(action.details), int(action.success), action.error,
            action.executed_at.isoformat()
        ))
        self._conn.commit()

    def save_health_snapshot(self, snapshot: HealthSnapshot) -> None:
        assert self._conn
        self._conn.execute("""
            INSERT INTO health_snapshots (id, app_id, region, health_score, health_status, raw_data, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.id, snapshot.app_id, snapshot.region,
            snapshot.health_score, snapshot.health_status.value,
            json.dumps(snapshot.raw_data), snapshot.recorded_at.isoformat()
        ))
        self._conn.commit()

    def get_last_health_snapshot(self, app_id: str, region: str) -> Optional[HealthSnapshot]:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM health_snapshots WHERE app_id=? AND region=? ORDER BY recorded_at DESC LIMIT 1",
            (app_id, region)
        ).fetchone()
        if row:
            row = dict(row)
            return HealthSnapshot(
                id=row["id"],
                app_id=row["app_id"],
                region=row["region"],
                health_score=row["health_score"],
                health_status=HealthStatus(row["health_status"]),
                raw_data=json.loads(row["raw_data"]),
                recorded_at=datetime.fromisoformat(row["recorded_at"]),
            )
        return None
