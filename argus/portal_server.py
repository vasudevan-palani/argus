"""Argus Applications Portal Server.

A separate FastAPI + static SPA web app that:
- Shows configured applications in a grid
- Provides an assistant chat to query config/incidents
- Allows executing incident actions via explicit UI confirmation

Start with: python3 -m argus.cli portal
"""

from __future__ import annotations

import re
import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from openai import OpenAI

from .config.loader import load_config
from .engine import MonitoringEngine
from .persistence.database import Database, IncidentState
from .tools.health import get_health

logger = structlog.get_logger(__name__)


def _load_dotenv() -> None:
    dotenv_path = os.environ.get("ARGUS_DOTENV_PATH")
    try:
        from dotenv import find_dotenv, load_dotenv
    except Exception:
        return

    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
        return

    load_dotenv(find_dotenv(usecwd=True), override=False)


_load_dotenv()


class ChatRequest(BaseModel):
    message: str
    app_id: Optional[str] = None
    history: list[dict[str, str]] = Field(default_factory=list)


class ChatAction(BaseModel):
    type: str
    label: str
    endpoint: str
    method: str = "POST"
    body: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    message: str
    actions: list[ChatAction] = Field(default_factory=list)


def create_portal_app(
    db_path: str = "argus.db",
    config_path: str = "monitor.config.yaml",
    dry_run: bool = True,
) -> FastAPI:
    app = FastAPI(title="Argus Portal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = db_path
    app.state.config_path = config_path
    app.state.dry_run = dry_run

    web_dir = Path(__file__).resolve().parent / "portal" / "web"
    index_html = web_dir / "index.html"

    client: Optional[OpenAI] = None
    if os.environ.get("OPENAI_API_KEY"):
        client = OpenAI()

    def _get_engine():
        cfg = load_config(app.state.config_path)
        db = Database(app.state.db_path)
        db.connect()
        return MonitoringEngine(config=cfg, db=db, dry_run=app.state.dry_run), db

    def _incident_to_dict(inc) -> dict[str, Any]:
        return {
            "id": inc.id,
            "app_id": inc.app_id,
            "app_name": inc.app_name,
            "region": inc.region,
            "health_score": inc.health_score,
            "health_status": inc.health_status.value,
            "state": inc.state.value,
            "created_at": inc.created_at.strftime("%Y-%m-%d %H:%M"),
            "summary": inc.summary,
            "failover_proposed": bool(getattr(inc, "failover_proposed", False)),
            "failover_from_region": getattr(inc, "failover_from_region", None),
            "failover_to_region": getattr(inc, "failover_to_region", None),
        }

    def _active_incident_for_app(app_id: str) -> Optional[dict[str, Any]]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            incidents = db.list_incidents(app_id=app_id, limit=25)
        finally:
            db.close()

        for inc in incidents:
            if inc.state in (IncidentState.RESOLVED, IncidentState.ACTION_EXECUTED):
                continue
            return _incident_to_dict(inc)
        return None

    def _get_model_name() -> str:
        try:
            cfg = load_config(app.state.config_path)
            return cfg.global_config.ai_model
        except Exception:
            return "gpt-4o"

    def _tool_spec(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        }

    def _tools() -> list[dict[str, Any]]:
        return [
            _tool_spec(
                "list_apps",
                "List configured applications.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            _tool_spec(
                "get_app_config",
                "Get a single application's configuration.",
                {
                    "type": "object",
                    "properties": {"app_id": {"type": "string"}},
                    "required": ["app_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_spec(
                "get_app_incidents",
                "List incidents for an application.",
                {
                    "type": "object",
                    "properties": {
                        "app_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["app_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_spec(
                "get_active_incident",
                "Get the most recent non-terminal incident for an application.",
                {
                    "type": "object",
                    "properties": {"app_id": {"type": "string"}},
                    "required": ["app_id"],
                    "additionalProperties": False,
                },
            ),
            _tool_spec(
                "get_current_health",
                "Get current health snapshot for an app in a region.",
                {
                    "type": "object",
                    "properties": {"app_id": {"type": "string"}, "region": {"type": "string"}},
                    "required": ["app_id", "region"],
                    "additionalProperties": False,
                },
            ),
            _tool_spec(
                "propose_incident_action",
                "Propose an incident action to the UI as a button (does not execute).",
                {
                    "type": "object",
                    "properties": {
                        "incident_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["approve", "reject"]},
                        "label": {"type": "string"},
                    },
                    "required": ["incident_id", "action", "label"],
                    "additionalProperties": False,
                },
            ),
        ]

    def _handle_tool_call(name: str, args: dict[str, Any], actions_out: list[ChatAction]) -> Any:
        if name == "list_apps":
            cfg = load_config(app.state.config_path)
            return [{"id": a.id, "name": a.name, "topology": a.topology, "regions": a.get_all_regions()} for a in cfg.applications]

        if name == "get_app_config":
            app_id = args["app_id"]
            return get_app(app_id)

        if name == "get_app_incidents":
            app_id = args["app_id"]
            limit = int(args.get("limit") or 20)
            return list_app_incidents(app_id, limit=limit)

        if name == "get_active_incident":
            app_id = args["app_id"]
            return _active_incident_for_app(app_id)

        if name == "get_current_health":
            app_id = args["app_id"]
            region = args["region"]
            h = get_health(app_id, region)
            return h.model_dump()

        if name == "propose_incident_action":
            incident_id = args["incident_id"]
            action = args["action"]
            label = args["label"]
            if action == "approve":
                actions_out.append(ChatAction(
                    type="approve_incident",
                    label=label,
                    endpoint=f"/api/incidents/{incident_id}/approve",
                    method="POST",
                    body={},
                ))
            else:
                actions_out.append(ChatAction(
                    type="reject_incident",
                    label=label,
                    endpoint=f"/api/incidents/{incident_id}/reject",
                    method="POST",
                    body={},
                ))
            return {"ok": True}

        raise ValueError(f"Unknown tool: {name}")

    def _tool_args(tool_call: Any) -> dict[str, Any]:
        parsed = getattr(tool_call.function, "parsed_arguments", None)
        if isinstance(parsed, dict):
            return parsed
        raw = getattr(tool_call.function, "arguments", None)
        if isinstance(raw, str) and raw.strip():
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    def _approval_available_for_incident(incident_id: str) -> Optional[str]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            approval = db.get_approval_for_incident(incident_id)
        finally:
            db.close()
        if approval and not approval.expired and not approval.approved_at:
            return approval.token
        return None

    @app.get("/", response_class=FileResponse)
    def index():
        if not index_html.exists():
            raise HTTPException(status_code=500, detail="portal index.html not found")
        return FileResponse(index_html)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/status")
    def api_status() -> dict:
        return {"dry_run": bool(app.state.dry_run)}

    @app.get("/api/apps")
    def list_apps() -> list[dict[str, Any]]:
        cfg = load_config(app.state.config_path)
        out: list[dict[str, Any]] = []
        for a in cfg.applications:
            db = Database(app.state.db_path)
            db.connect()
            try:
                recent = db.list_incidents(app_id=a.id, limit=50)
            finally:
                db.close()

            open_incidents = [
                i for i in recent
                if i.state not in (IncidentState.RESOLVED, IncidentState.ACTION_EXECUTED)
            ]

            active_inc = _incident_to_dict(open_incidents[0]) if open_incidents else None
            pending_approval = bool(active_inc and active_inc.get("state") == "awaiting_approval")
            out.append({
                "id": a.id,
                "name": a.name,
                "team": getattr(a, "team", None),
                "topology": a.topology,
                "regions": a.get_all_regions(),
                "active_incident": active_inc,
                "open_incidents_count": len(open_incidents),
                "pending_approval": pending_approval,
            })
        return out

    @app.get("/api/apps/{app_id}")
    def get_app(app_id: str) -> dict[str, Any]:
        cfg = load_config(app.state.config_path)
        app_cfg = next((a for a in cfg.applications if a.id == app_id), None)
        if not app_cfg:
            raise HTTPException(status_code=404, detail="Unknown app")

        return {
            "id": app_cfg.id,
            "name": app_cfg.name,
            "team": getattr(app_cfg, "team", None),
            "topology": app_cfg.topology,
            "regions": app_cfg.get_all_regions(),
            "owners": {
                "primary": getattr(app_cfg.owners.primary, "model_dump", lambda: {})(),
                "secondary": getattr(app_cfg.owners.secondary, "model_dump", lambda: {})(),
            },
            "failover": getattr(app_cfg.failover, "model_dump", lambda: {})() if app_cfg.failover else None,
            "notifications": getattr(app_cfg.notifications, "model_dump", lambda: {})() if app_cfg.notifications else None,
            "escalation": [getattr(s, "model_dump", lambda: {})() for s in (app_cfg.escalation or [])],
            "dependencies": [getattr(d, "model_dump", lambda: {})() for d in (app_cfg.dependencies or [])],
        }

    @app.get("/api/apps/{app_id}/incidents")
    def list_app_incidents(app_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            total = db.count_incidents(app_id=app_id)
            incidents = db.list_incidents_paginated(app_id=app_id, limit=limit, offset=offset)
        finally:
            db.close()
        items = [_incident_to_dict(i) for i in incidents]
        has_more = (offset + len(items)) < total
        return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": has_more}

    @app.get("/api/apps/{app_id}/notifications")
    def list_app_notifications(app_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            total = db.count_notification_attempts(app_id=app_id)
            attempts = db.list_notification_attempts_paginated(app_id=app_id, limit=limit, offset=offset)
        finally:
            db.close()

        items = []
        for a in attempts:
            items.append({
                "id": a.id,
                "incident_id": a.incident_id,
                "channel": a.channel,
                "target": a.target,
                "phone_or_webhook": a.phone_or_webhook,
                "success": bool(a.success),
                "error": a.error,
                "attempted_at": a.attempted_at.isoformat(),
                "message": a.message,
            })

        has_more = (offset + len(items)) < total
        return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": has_more}

    @app.post("/api/incidents/{incident_id}/approve")
    def approve_incident(incident_id: str, operator: str = "portal") -> dict[str, Any]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            approval = db.get_approval_for_incident(incident_id)
        finally:
            db.close()
        if not approval or approval.expired or approval.approved_at:
            raise HTTPException(status_code=404, detail="No active approval for incident")

        engine, db2 = _get_engine()
        try:
            result = engine.process_approval(approval.token, approved_by=operator)
        finally:
            db2.close()
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Approval failed"))
        return result

    @app.post("/api/incidents/{incident_id}/reject")
    def reject_incident(incident_id: str, operator: str = "portal") -> dict[str, Any]:
        db = Database(app.state.db_path)
        db.connect()
        try:
            approval = db.get_approval_for_incident(incident_id)
            if not approval:
                raise HTTPException(status_code=404, detail="No approval for incident")
            incident = db.get_incident_by_id(approval.incident_id)
            if not incident:
                raise HTTPException(status_code=404, detail="Incident not found")
            if incident.state.value == "awaiting_approval":
                incident.state = IncidentState.RESOLVED
                incident.resolved_at = datetime.now(timezone.utc)
                db.save_incident(incident)
            approval.expired = True
            approval.approved_by = operator
            db.save_approval(approval)
        finally:
            db.close()
        return {"success": True}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(req: ChatRequest) -> ChatResponse:
        msg = (req.message or "").strip()
        if not msg:
            return ChatResponse(message="Ask about an application, configuration, or incidents.")

        if client is None:
            return ChatResponse(
                message=(
                    "OpenAI is not configured. Set OPENAI_API_KEY to enable the intelligent assistant.\n"
                    "You can still use the portal UI buttons for approve/reject."
                )
            )

        actions_out: list[ChatAction] = []

        cfg = load_config(app.state.config_path)
        apps = {a.id: a for a in cfg.applications}
        selected_app_id = req.app_id if req.app_id in apps else None

        system = (
            "You are the Argus Portal assistant. Your job is to help the user understand application configuration, "
            "owners/contacts, current incidents, and current health. Use the provided tools to fetch facts. "
            "If the user asks to take an action (approve/reject), DO NOT execute it directly; instead call "
            "propose_incident_action so the UI shows a button. Be concise and operational."
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for h in (req.history or [])[-12:]:
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})

        context_line = f"Selected app context: {selected_app_id}" if selected_app_id else "Selected app context: none"
        messages.append({"role": "user", "content": f"{context_line}\nUser message: {msg}"})

        model = _get_model_name()

        try:
            for _ in range(6):
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=_tools(),
                    tool_choice="auto",
                )
                choice = resp.choices[0]
                tool_calls = getattr(choice.message, "tool_calls", None)

                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": choice.message.content or "",
                        "tool_calls": [tc.model_dump() for tc in tool_calls],
                    })
                    for tc in tool_calls:
                        name = tc.function.name
                        args = _tool_args(tc)
                        try:
                            result = _handle_tool_call(name, args, actions_out)
                        except Exception as e:
                            result = {"error": str(e)}
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, default=str),
                        })
                    continue

                text = choice.message.content or ""
                return ChatResponse(message=text.strip() or "OK", actions=actions_out)

            return ChatResponse(message="I hit a tool-calling loop limit. Try rephrasing.", actions=actions_out)
        except Exception as e:
            logger.warning("portal_chat_openai_failed", error=str(e))
            return ChatResponse(
                message="Assistant is temporarily unavailable (OpenAI call failed).",
                actions=actions_out,
            )

    return app
