# Stream Large Sweeps to Control Worker RSS

## Context
- Analyzer sweeps (`plan_missing_ci`, dependency rebuild) iterate many PRs on large repos (e.g., mathlib4).
- Django’s default queryset iteration caches rows, keeping all model instances in memory for the life of the task.
- On Heroku’s 512 MB dyno with Celery concurrency>1, cached sweeps plus other tasks pushed dyno RSS over quota.

## Decision
- Stream sweep querysets with `.iterator(...)` and narrow column selection via `.only(...)` and `select_related(...)` for required relations.
- Apply this to:
  - `analyzer.plan_missing_ci` sweep of PRs.
  - `analyzer.rebuild_dependencies_sweep` sweep of PR bodies.
  - `analyzer.rebuild_revisions_sweep` (PRRevision rebuild) PR loop.
  - `analyzer.rebuild_queue_windows_sweep` (queue windows) PR loop.
- Keep ordering and limits unchanged; logic and outputs stay the same.

## Consequences
- Lower per-task RSS by avoiding queryset caching and wide column loads.
- Slightly more DB I/O (no queryset cache), acceptable given memory headroom gains.
- Sweep throughput per pass may drop marginally, but overall stability improves; fan-out/batch knobs remain available for further tuning.

## Operational Notes
- No migrations or env changes required; deploy code change only.
- If memory pressure persists, tune env knobs to shrink sweep batch sizes or disable dependency fanout.
- Iterator chunk size is 100 by default; adjust if specific workloads need finer batching.
- Django caching note:
  - Without `.iterator()`, a queryset fills `_result_cache` as you iterate and retains every row until the queryset is GC’d, so per-task RSS grows with the slice size.
  - `.iterator()` bypasses `_result_cache` and discards rows after yielding; `chunk_size` only controls fetch size per DB round trip and does not reintroduce caching.

## Alternatives
- Keep existing iteration and rely only on smaller batch sizes/concurrency limits (reduces throughput more and still risks spikes).
- Split sweeps into smaller tasks via fanout everywhere (adds scheduling overhead and still needs streaming to keep parent tasks lean).
