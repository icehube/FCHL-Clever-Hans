# FCHL Auction Manager

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

## Stack

**FastAPI** + **HTMX** + **Jinja2** + **PuLP** (MILP solver)

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --reload
# Opens at http://localhost:8000
```

## Development

```bash
.venv/bin/pytest tests/ -v
```

## Documentation

- [CLAUDE.md](CLAUDE.md) — full project spec: architecture, endpoints, league rules, design rationale, and coding conventions
- [BACKLOG.md](BACKLOG.md) — open findings and future work
