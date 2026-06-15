"""V2 — independent depletion reference model (the centerpiece correctness proof).

An in-memory ``World`` (items / recipes / modifiers / conversions) + an ``oracle`` that
recomputes expected per-(item) net δ and on-hand in pure Python/Decimal — **without ever
importing ``app.modules.inventory.depletion``** — plus a ``seed``er and ``run``ner that drive
the REAL engine over the same World and read back the actual ledger / on-hand to compare.

Independence is the whole point: the production engine's own phase tests share its SQL and
its Decimal operation order, so they cannot catch a shared-assumption bug. This oracle is a
clean-room reimplementation of the v5 §11 formula. It is reused by V5 (the 2,000-order
restaurant simulation) at scale — V5 generates a large random World, runs the full worker,
and compares against this same oracle.

See ``docs/sprints/v1-failure-class-matrix.md`` → "V2 — independent depletion oracle".
"""
