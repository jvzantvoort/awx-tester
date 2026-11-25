# Writing a check

Checks live in `src/awx_tester/checks/`. A check is a function registered with `@check`:

```python
from ..model import Better, Level
from ..registry import Skip, check


@check("platform.licenses", "Subscription is valid", requires=("api", "auth"), tags=("smoke",))
def licenses(ctx, out):
    """One line that list-checks shows."""
    info = ctx.api.get("config/").get("license_info") or {}
    if not info:
        raise Skip("no subscription information (AWX and Ascender have none)")
    out.attr("license_type", info.get("license_type"))
    out.metric("days_remaining", info.get("time_remaining", 0) / 86400, "days", Better.HIGHER)
    out.threshold("days remaining", info.get("time_remaining", 0) / 86400, warn=30, fail=0,
                  unit=" days", higher_is_worse=False)
```

## The contract

- **id** is `category.name`, lower case, and stable for ever (see [report-format.md](report-format.md)).
- **level**:
  - `readonly` may only issue GETs and Kubernetes list or get calls;
  - `mutating` may create objects, through `ctx.create()` or `ctx.track()` so that they are cleaned up, named with `ctx.name_prefix`;
  - `disruptive` may break things, but must first check that the component is healthy.
- **requires**: any of `api`, `auth`, `superuser`, `kube`, `namespace`. The runner skips the
  check with a reason when one is missing.
- **depends**: ids of checks that must have passed first. They are pulled into `--only`
  selections automatically.
- Record:
  - `out.metric(name, value, unit, better, description)` for anything comparable;
  - `out.attr(key, value)` for evidence;
  - `out.detail(text)` for log-like lines.
- Judge:
  - `out.warn(msg)` / `out.fail(msg)`, which can be called several times (the worst wins);
  - `out.threshold(...)` for configured limits;
  - `raise Skip(reason)` when the check does not apply.
  - Exceptions become `error` with a short traceback, so do not catch broadly.
- Tunables go in `DEFAULT_OPTIONS` in `context.py` and are read with `ctx.opt(section, key)`.
- Data collected once per run is in `ctx.env` (`platform`, `cluster`). Use it rather than
  querying the same thing again.
- To share objects between checks (for example a job template), use `ctx.shared`.

Register new modules in `checks/__init__.py`. The import order is the run order within a level.

## Testing

Unit tests mock the controller with `responses` (see `tests/test_runner.py`). Run them with:

```bash
.venv/bin/pytest -q && .venv/bin/ruff check src tests
```

Before proposing a mutating or disruptive check, run it against a disposable installation and
confirm that `awx-tester` leaves no `awx-tester-*` objects behind.
