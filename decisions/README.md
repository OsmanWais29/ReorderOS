# Decisions

Sprint 0 deliverables. These documents are the canonical scope contract for v1; sprints reference and update them but do not duplicate them.

| File                              | Purpose                                                                                 |
|-----------------------------------|-----------------------------------------------------------------------------------------|
| `v1-scope.md`                     | Frozen product/architecture locks for the Clover-only App Store launch.                 |
| `contradiction-register.md`       | Disagreements between docs, with resolutions. New contradictions append here.           |
| `release-risk-register.md`        | Pre-launch risks, severity, mitigations, re-score triggers.                             |
| `api-surface-inventory.md`        | Every screen → endpoint mapping, plus background task surface. Drives OpenAPI scope.    |
| `bilingual-string-inventory.md`   | EN/FR coverage by surface; CI rule lives in Sprint 10.                                  |
| `adr/`                            | (future) Architecture Decision Records for any post-Sprint-0 deviations.                |

Rules:

- Any change to a v1 lock requires a new ADR under `adr/` referencing the section it supersedes.
- Sprint PRs reference the section(s) they implement in the PR description.
- The contradiction register cannot have `Open` rows in a sprint's dependency chain at exit time.
