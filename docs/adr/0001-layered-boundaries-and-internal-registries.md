# ADR 0001: Layered boundaries and application-owned selection

- Status: Accepted
- Scope: P0 issue #17

## Decision

Keep framework-neutral contracts, validated registries, and worker lifecycle
primitives in `core`. Keep file decoding and session ownership outside Qt
widgets. Let `app` compose concrete providers and own provider selection.

Use the registry only for concrete implementations that the application
actually selects. The current CSV importer is registered at the composition
root. Add source or widget registries when there are real implementations that
need that selection; do not create empty extension points in advance.

Workers hand immutable event snapshots and failure details to their owner.
Qt objects remain on the UI thread, and cancellation is cooperative.

## Consequences

- Core can be imported and tested without Qt.
- Importing a file and retaining the normalized session happen in the
  application controller; widgets receive presentation data.
- Duplicate, unknown, padded, and failing registry entries have explicit,
  deterministic behavior.
- Adding an implementation requires composition code, not widget changes.
- There is no public plugin SDK, dynamic code loading, or speculative provider
  framework in this milestone.
