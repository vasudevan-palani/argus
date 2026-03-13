# Argus — AI Platform Health Monitoring Service

Argus is a **config-driven AI platform health monitoring system** that evaluates the operational health of applications deployed across cloud regions, correlates failures with AWS service outages, and coordinates notifications and remediation recommendations.

## Architecture

```
argus/
├── argus/
│   ├── agent/             # AI agent orchestration
│   │   ├── health_evaluator.py   # Deterministic health scoring
│   │   └── orchestrator.py       # AI reasoning (GPT-4o + fallback)
│   ├── config/            # Configuration loading & validation
│   │   └── loader.py
│   ├── engine.py          # Main monitoring engine
│   ├── cli.py             # CLI interface (typer)
│   ├── notifications/     # Notification & escalation
│   │   └── escalation.py
│   ├── persistence/       # SQLite data layer
│   │   └── database.py
│   └── tools/             # Operational tool integrations
│       ├── health.py             # get_health()
│       ├── aws_outage.py         # get_aws_services_outage()
│       ├── traffic.py            # flip_application_traffic()
│       └── notification.py       # send_notification()
├── monitor.config.yaml    # Application configuration
├── pyproject.toml
└── README.md
```

## Installation

```bash
cd argus
pip install -e ".[dev]"
```

## Configuration

All application monitoring is configured in `monitor.config.yaml`. See the example for full schema.

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
# Edit .env with your API keys
```

## Usage

### Validate configuration
```bash
argus check
argus check --config monitor.config.yaml
```

### Run a single monitoring cycle
```bash
argus monitor --once
argus monitor --once --no-dry-run   # Enable real traffic flips
```

### Run continuous monitoring
```bash
argus monitor
argus monitor --log-level DEBUG
```

### View current status
```bash
argus status
```

### List incidents
```bash
argus incidents
argus incidents --app checkout-service
argus incidents --limit 50
```

### Approve a failover
```bash
argus approve <approval-token>
argus approve <token> --operator "John Doe" --no-dry-run
```

## Health Scoring

| Score | Status   |
|-------|----------|
| 90–100 | Healthy  |
| 70–89  | Degraded |
| 0–69   | Down     |

Scores are calculated deterministically from:
- **Availability** (0–30 pts)
- **Error rate** (0–25 pts)
- **Latency P99** (0–20 pts)
- **Active alarms** (0–15 pts)
- **Dependency health** (0–10 pts)

AI (GPT-4o) is used for incident summarization and recommendations. The deterministic score is always the source of truth.

## Testing Scenarios

Use environment variables to simulate different health scenarios:

```bash
# Make checkout-service degraded in us-east-1
export ARGUS_HEALTH_CHECKOUT_SERVICE_US_EAST_1_SCENARIO=degraded

# Make checkout-service passive region healthy
export ARGUS_HEALTH_CHECKOUT_SERVICE_US_WEST_2_SCENARIO=healthy

# Simulate AWS RDS outage in us-east-1
export ARGUS_AWS_OUTAGE_RDS_US_EAST_1_STATUS=degraded

# Run a single cycle to see results
argus monitor --once
```

## Notification Channels

- **Teams**: Via webhook URL (per-app config or env var)
- **SMS**: Via Twilio (set `TWILIO_*` env vars)
- **Voice Call**: Via Twilio

Without Twilio credentials, SMS/calls are logged to stdout as dry-run output.

## Safety

- Traffic flips NEVER execute without explicit human approval
- All actions are auditable via SQLite database
- AI cannot override deterministic health scores
- Approval tokens expire after configured timeout
- Dry-run mode enabled by default (`--dry-run`)

## Escalation Flow

1. Immediate → Teams (primary)
2. +5 min → SMS (primary)
3. +10 min → Voice call (primary)
4. +15 min → SMS (secondary)

Escalation stops when incident is acknowledged or resolved.
