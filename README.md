# FCHL Auction Simulator

A live auction draft tool for an 11-team fantasy hockey league. During a multi-hour, 150+ pick auction, the simulator tracks all teams, computes market-adjusted bid limits, recommends nominations, provides real-time bidding advice, evaluates trades and buyouts on the fly, and recalculates the ideal roster after every transaction.

## Stack

**FastAPI** + **HTMX** + **Jinja2** + **PuLP** (MILP solver)

## Quick Start

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Opens at http://localhost:8000
```

## Development

```bash
pytest tests/ -v
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for the full project spec: architecture, module docs, data formats, league rules, and coding conventions.
