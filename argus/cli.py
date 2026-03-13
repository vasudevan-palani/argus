"""Argus CLI - Command-line interface for the monitoring system."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import structlog
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from .config.loader import load_config
from .persistence.database import Database
from .engine import MonitoringEngine


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

app = typer.Typer(
    name="argus",
    help="Argus AI Platform Health Monitoring System",
    add_completion=False,
)
console = Console()


def _setup_logging(log_level: str = "INFO") -> None:
    """Configure structured JSON logging."""
    import logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


@app.command()
def monitor(
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Dry run mode (no real traffic flips)"),
    once: bool = typer.Option(False, "--once", help="Run a single monitoring cycle and exit"),
    log_level: str = typer.Option("INFO", "--log-level", help="Log level"),
) -> None:
    """Start the Argus monitoring engine."""
    _setup_logging(log_level)

    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    console.print(Panel.fit(
        f"[bold blue]Argus[/bold blue] AI Platform Health Monitor\n"
        f"Apps: {len(cfg.applications)} | "
        f"Model: {cfg.global_config.ai_model} | "
        f"Dry Run: {dry_run}",
        title="Starting"
    ))

    database = Database(db)
    database.connect()
    try:
        engine = MonitoringEngine(config=cfg, db=database, dry_run=dry_run)
        if once:
            grace = float(os.environ.get("ARGUS_ONCE_NOTIFICATION_GRACE_SECONDS", "2.5"))
            results = engine.run_once(notification_grace_seconds=grace)
            _print_results(results)
        else:
            try:
                engine.run_continuous()
            except KeyboardInterrupt:
                console.print("\n[yellow]Monitoring stopped.[/yellow]")
    finally:
        database.close()


def _print_results(results: dict) -> None:
    """Print monitoring results in a readable table."""
    table = Table(title="Health Check Results", show_header=True)
    table.add_column("Application", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Score")
    table.add_column("Incident ID")
    table.add_column("State")

    status_colors = {"healthy": "green", "degraded": "yellow", "down": "red", "error": "red"}

    for app_id, result in results.items():
        if "error" in result and "regions" not in result:
            table.add_row(app_id, "[red]ERROR[/red]", "-", "-", result["error"])
            continue

        regions = result.get("regions", {})
        for region, region_result in regions.items():
            if isinstance(region_result, dict) and "error" not in region_result:
                status = region_result.get("health_status", "unknown")
                score = f"{region_result.get('score', 0):.0f}/100"
                incident_id = region_result.get("incident_id", "-")
                if incident_id and incident_id != "-":
                    incident_id = incident_id[:8] + "..."
                state = region_result.get("state", "-")
                color = status_colors.get(status, "white")
                table.add_row(
                    f"{app_id} ({region})",
                    f"[{color}]{status.upper()}[/{color}]",
                    score,
                    incident_id,
                    state,
                )

    console.print(table)


@app.command()
def check(
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
) -> None:
    """Validate the Argus configuration file."""
    try:
        cfg = load_config(config)
        console.print(f"[green]✓[/green] Configuration valid: {config}")
        console.print(f"  Applications: {len(cfg.applications)}")
        for app_cfg in cfg.applications:
            regions = app_cfg.get_all_regions()
            console.print(f"  - {app_cfg.name} ({app_cfg.id}): {', '.join(regions)}")
    except FileNotFoundError:
        console.print(f"[red]Error:[/red] Config file not found: {config}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] Invalid configuration: {e}")
        raise typer.Exit(1)


@app.command()
def approve(
    token: str = typer.Argument(..., help="Approval token for failover"),
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Dry run mode"),
    operator: str = typer.Option("operator", "--operator", help="Name of the operator approving"),
) -> None:
    """Approve a pending failover request."""
    _setup_logging()

    database = Database(db)
    database.connect()
    try:
        cfg = load_config(config)
        engine = MonitoringEngine(config=cfg, db=database, dry_run=dry_run)
        result = engine.process_approval(token, approved_by=operator)
        if result["success"]:
            console.print(f"[green]✓[/green] Approval processed successfully")
            console.print(f"  Incident: {result.get('incident_id', 'unknown')}")
            console.print(f"  Message: {result.get('message', '')}")
            if result.get("dry_run"):
                console.print(f"  [yellow]Note: Dry run mode — no actual traffic was changed.[/yellow]")
        else:
            console.print(f"[red]✗[/red] Approval failed: {result.get('error', 'Unknown error')}")
    finally:
        database.close()


@app.command()
def incidents(
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
    app_id: Optional[str] = typer.Option(None, "--app", "-a", help="Filter by application ID"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of incidents to show"),
) -> None:
    """List recent incidents."""
    database = Database(db)
    database.connect()
    try:
        incident_list = database.list_incidents(app_id=app_id, limit=limit)

        table = Table(title="Recent Incidents", show_header=True)
        table.add_column("ID", style="dim")
        table.add_column("Application")
        table.add_column("Region")
        table.add_column("Status", justify="center")
        table.add_column("Score")
        table.add_column("State")
        table.add_column("Created")

        state_colors = {
            "detected": "yellow",
            "notified": "blue",
            "acknowledged": "cyan",
            "awaiting_approval": "magenta",
            "approved": "green",
            "action_executed": "green",
            "resolved": "green",
            "escalated": "red",
        }

        for inc in incident_list:
            color = state_colors.get(inc.state.value, "white")
            table.add_row(
                inc.id[:8] + "...",
                inc.app_name,
                inc.region,
                inc.health_status.value.upper(),
                f"{inc.health_score:.0f}/100",
                f"[{color}]{inc.state.value}[/{color}]",
                inc.created_at.strftime("%m-%d %H:%M"),
            )

        console.print(table)
    finally:
        database.close()


@app.command()
def status(
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
) -> None:
    """Show current status of all monitored applications."""
    _setup_logging("WARNING")

    try:
        cfg = load_config(config)
    except FileNotFoundError:
        console.print(f"[red]Config not found:[/red] {config}")
        raise typer.Exit(1)

    database = Database(db)
    database.connect()
    try:
        table = Table(title="Argus Application Status", show_header=True)
        table.add_column("Application")
        table.add_column("Region")
        table.add_column("Last Score")
        table.add_column("Last Status")
        table.add_column("Active Incident")
        table.add_column("Last Checked")

        for app_cfg in cfg.applications:
            for region in app_cfg.get_all_regions():
                snapshot = database.get_last_health_snapshot(app_cfg.id, region)
                incident = database.get_active_incident(app_cfg.id, region)

                score = f"{snapshot.health_score:.0f}/100" if snapshot else "-"
                status_val = snapshot.health_status.value if snapshot else "unknown"
                last_checked = snapshot.recorded_at.strftime("%m-%d %H:%M") if snapshot else "-"
                has_incident = f"[red]{incident.id[:8]}...[/red]" if incident else "[green]None[/green]"

                status_colors = {"healthy": "green", "degraded": "yellow", "down": "red"}
                sc = status_colors.get(status_val, "white")

                table.add_row(
                    app_cfg.name,
                    region,
                    score,
                    f"[{sc}]{status_val.upper()}[/{sc}]",
                    has_incident,
                    last_checked,
                )

        console.print(table)
    finally:
        database.close()


@app.command()
def serve(
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host to bind the server to"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Dry run mode (no real traffic flips)"),
) -> None:
    """Start the Argus approval web server.

    Set ARGUS_SERVER_URL=http://<your-host>:<port> so Teams cards include
    a clickable 'Approve Failover' button linking to this server.
    """
    _setup_logging("WARNING")

    from .server import create_app

    server_url = os.environ.get("ARGUS_SERVER_URL", f"http://{host}:{port}")
    console.print(Panel.fit(
        f"[bold blue]Argus[/bold blue] Approval Server\n"
        f"Listening on: [green]{server_url}[/green]\n"
        f"Dashboard:    [cyan]{server_url}/[/cyan]\n"
        f"Dry Run: {dry_run}\n\n"
        f"[dim]Set ARGUS_SERVER_URL={server_url} so Teams cards link here.[/dim]",
        title="Argus Server",
    ))

    fastapi_app = create_app(db_path=db, config_path=config, dry_run=dry_run)
    import uvicorn
    uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")


@app.command()
def portal(
    config: str = typer.Option("monitor.config.yaml", "--config", "-c", help="Path to config file"),
    db: str = typer.Option("argus.db", "--db", help="Path to SQLite database"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host to bind the server to"),
    port: int = typer.Option(8081, "--port", "-p", help="Port to listen on"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Dry run mode (no real traffic flips)"),
) -> None:
    """Start the Argus applications portal web app."""
    _setup_logging("WARNING")

    from .portal_server import create_portal_app

    server_url = f"http://{host}:{port}" if host not in ("0.0.0.0", "::") else f"http://localhost:{port}"
    console.print(Panel.fit(
        f"[bold blue]Argus[/bold blue] Applications Portal\n"
        f"Listening on: [green]{server_url}[/green]\n"
        f"Portal:       [cyan]{server_url}/[/cyan]\n"
        f"Dry Run: {dry_run}",
        title="Argus Portal",
    ))

    fastapi_app = create_portal_app(db_path=db, config_path=config, dry_run=dry_run)
    import uvicorn
    uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
