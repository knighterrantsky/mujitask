# Real Migration Validation

Use these checks during `real_migration` work. They are intentionally separate
from the old pytest suite, so migration cleanup does not depend on old imports,
monkeypatch paths, or compatibility shims.

## Static ownership

Run:

```bash
uv run python scripts/dev/check_real_migration_ownership.py
```

The check fails on:

- `sys.modules[__name__]` aliases anywhere under `src/`.
- Wildcard imports and `noqa: F401,F403` re-export shims anywhere under `src/`.
- Thin non-`__init__.py` modules that only import symbols and publish them via
  `__all__`.
- Empty migration-note modules in the runtime main path.
- A present `src/automation_business_scaffold/capabilities/_implementations`
  directory.
- Legacy domain aggregate modules such as
  `domains/tiktok/mappers/feishu_source_adapters.py` and
  `domains/tiktok/projections/feishu_projection_mappers.py`.
  Specific mapper/projection modules must own the implementation; `registry.py`
  is only for lookup by stable code.
- Imports from `automation_business_scaffold.business.*` in the runtime main
  path: `apps`, `control_plane`, `domains`, `capabilities`, and `contracts`.
- Console scripts that still target legacy root process modules instead of
  `apps/**` entrypoints.

## Achieve behavior comparison

Run all bundled fixture comparisons:

```bash
uv run python scripts/dev/compare_achieve_acceptance_artifacts.py
```

Run one fixture and write normalized artifacts plus a diff report:

```bash
uv run python scripts/dev/compare_achieve_acceptance_artifacts.py \
  --workflow-code refresh_current_competitor_table \
  --scenario-id competitor_row_refresh_minimal \
  --artifact-dir runtime/acceptance_comparison
```

Compare one fixture against fresh runtime artifacts without invoking pytest:

```bash
uv run python scripts/dev/compare_achieve_acceptance_artifacts.py \
  --workflow-code refresh_current_competitor_table \
  --scenario-id competitor_row_refresh_minimal \
  --candidate-request-payload /path/to/request-payload.json \
  --candidate-runtime-trace /path/to/runtime-trace.json \
  --candidate-fact-projection /path/to/fact-projection.json \
  --candidate-feishu-projection /path/to/feishu-projection.json \
  --candidate-outbox /path/to/outbox.json \
  --artifact-dir runtime/acceptance_comparison
```

The comparator reuses `automation_business_scaffold.acceptance.comparator` and
the `tests/fixtures/achieve_acceptance` payload contract. It reads JSON
artifacts only; it does not run the old runtime pytest harness.
