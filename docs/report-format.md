# Report format

Each run writes one `report.json`. The JSON Schema is in
[`src/awx_tester/report.schema.json`](../src/awx_tester/report.schema.json) (`awx-tester schema`
prints it). The HTML report embeds the same document in
`<script type="application/json" id="awx-tester-report">`.

## Top level

| Key | Contents |
|---|---|
| `schema_version` | `"1.0"`. Minor versions only add fields. A new major version means old reports cannot be compared |
| `tool` | `name`, `version`, `python` |
| `run` | `id`, `started`, `finished`, `duration_s`, `level`, `checks` (selected ids), `options` (effective tunables), `notes`, `unavailable` (requirement → reason), `cleanup_problems`, `kept_objects`, `redacted` |
| `target` | `name`, `type`, `url`, `namespace`, `kube_context`, `instance`, `labels`, `protected`, `max_level` |
| `environment.platform` | `type`, `display`, `api_prefix`, `version`, `gateway_version` (AAP), `ha`, `instances`, `instance_groups`, `me`, `config`, `scale` (object counts), `settings` (selected), `errors` |
| `environment.cluster` | `context`, `distribution`, `kubernetes_version`, `nodes[]`, `totals`, `storage_classes[]`, `ingress_classes[]`, `node_usage`, `installation`, `errors` |
| `environment.cluster.installation` | `name` (namespace), `instance`, `custom_resource` (`kind`, `name`, `spec`, `version`, `conditions`), `operator`, `deployments[]`, `statefulsets[]`, `pods[]`, `pvcs[]`, `ingresses[]`, `pod_usage` |
| `summary` | counts per status, `total`, `overall` (the worst status) |
| `results[]` | one entry per check, see below |

Byte quantities are in bytes, CPU in cores, durations in seconds unless a metric's unit says otherwise.

## Results

```json
{
  "id": "perf.api_latency",
  "title": "API read latency",
  "category": "perf",
  "level": "readonly",
  "tags": ["performance"],
  "status": "pass",
  "duration_s": 54.2,
  "message": "",
  "details": [],
  "metrics": [
    {"name": "p95_ms", "value": 412.3, "unit": "ms", "better": "lower", "description": ""}
  ],
  "attributes": {"endpoints": [{"endpoint": "ping/", "p50": 21.0, "p95": 28.5}]}
}
```

- `status`:
  - `pass`
  - `warn`: works, but beyond a warn threshold or suspicious
  - `fail`: does not work, or beyond a fail threshold
  - `error`: the check itself crashed
  - `skip`: a requirement or dependency is missing, with the reason in `message`
- `metrics` are what comparisons use. `better` is `lower`, `higher` or `none` (informational).
- `attributes` are free-form evidence (tables, timelines) shown in the HTML report. They are not compared.

## Stability rules

- A check id is never reused for a different measurement. If a check's meaning changes, it gets
  a new id.
- A metric name within a check keeps its unit and meaning. Rename it if either changes.
- New checks, metrics and fields may appear in any minor version. Consumers must ignore unknown keys.

## How `compare` decides

The first report is the baseline. For each other report:

- **Environment**: the fingerprint (see `fingerprint.py`) is flattened to `key → value`, and
  any difference is marked *changed*.
- **Check outcomes**: a worse status is *regressed*, a better one *improved*, and a change to or
  from `skip` is *changed*.
- **Metrics**: the relative change must exceed `--threshold` (10 % by default) and an absolute
  noise floor for the unit (`ms` 5, `s` 0.5, `%` 1, rates 1). Then:
  - with `better: lower` or `higher` it is *improved* or *regressed*;
  - with `better: none` it is *changed*.
- **Warnings** appear when options, level or tool version differ from the baseline.

`compare --json` writes the comparison itself as a document. `--fail-on-regression` makes it
usable as a CI gate.
