"""Observability: the one-way event stream (designs/observability.md).

Every component writes events through `observability.emit`; only this package
may read the `events` table. Trading code never reads events (#56).
"""
