# Boomer — Design Documents

This folder contains the complete design specification for **Boomer**, a scheduled autonomous trading research and execution bot for Indian equity markets (BSE + NSE).

The design was produced through a structured, deliberate process: every component was specified on paper before any code was written. The goal was to surface architectural decisions, identify failure modes, and lock down contracts between components — so that the eventual implementation becomes mechanical translation rather than discovery.

## Reading order

For a first read, follow this order:

1. **`design-evolution.md`** — the narrative of how the design came to be. Read this first if you want context on *why* decisions were made the way they were. It captures the pushbacks, reversals, and refinements that shaped the architecture.

2. **`phase-1-capital-and-risk.md`** — the foundation. Capital allocation, circuit breakers, high-water mark mechanics, self-funding flow. Everything else depends on this.

3. **`phase-2-collector.md`** — System 1. How raw market and corporate data is ingested, archived, normalised, and made point-in-time correct.

4. **`phase-3-brain.md`** — System 2 internals. Feature store, regime detection, three signal tracks (long-term / swing / intraday), trade decision layer, entry timing classifiers, portfolio constructor, APM gate.

5. **`phase-4-executor-and-backtesting.md`** — System 3 plus the backtesting harness, intraday continuous pipeline, and Stage 4b position review.

6. **`phase-5-orchestrator-dashboard-ops.md`** — how the system actually runs. Scheduler, dashboard views, deployment, monitoring, runbook.

## Design principles

The design is opinionated. Five principles shaped every decision:

1. **Boring monolith over distributed system.** One process, one database file, one VM — until empirical scale demands more. Microservices, message queues, and orchestration frameworks are explicitly avoided.

2. **Risk first, returns second.** The four-layer risk model and circuit breakers are designed to ensure structural inability to suffer catastrophic loss, not to maximise returns.

3. **Explicit attribution everywhere.** Every signal, decision, and trade carries the chain of reasoning that produced it. This is what enables learning, debugging, and the eventual ML migration.

4. **Same code, two modes.** The backtester is the live system with the broker swapped for a simulator. No "backtest version" of the logic that diverges from production.

5. **Scale invariance.** No rupee amounts hardcoded. Every threshold is a percentage or function of capital, so the system works identically at ₹50,000 or ₹50,00,000.

## Status

Design phase: **complete**. Five phases closed, all decisions signed off, ~40 loopholes identified and addressed inline within each phase doc.

Implementation phase: **not started**. Next step is translating these designs into code, component by component, in the order specified by the design evolution.

## Important context

This system trades real money in Indian equity markets. The design includes structural safeguards against the most common failure modes in retail algorithmic trading. However:

- The system does not generate alpha by itself. Edge comes from signal quality.
- Compliance with SEBI algo trading regulations and broker-specific rules (Zerodha Kite Connect API terms) is the operator's responsibility.
- This is not financial advice. Trading carries risk of loss.

## Repository

Project repository: `https://github.com/ps-pzts/boomer.git`
