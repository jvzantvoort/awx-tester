# Design

## Goal

Give operators one cohesive picture of an automation controller installation (AWX, AAP or
Ascender): does it work, how fast is it, what is it running on. The picture must be comparable
across clusters, across versions of one cluster, and between people who never share a network.

## Shape

```text
 targets.yml ──► config ──► safety gate ──► Runner ──► Report (JSON, canonical)
                                             │  │            │
                       controller REST API ◄─┘  └─► Kubernetes (list/get; delete pods only when disruptive)
                                                             ▼
                                   console · HTML · xUnit · compare (N reports)
```

- **Target**: one installation: platform type, URL, credentials, kube context and namespace,
  labels, and `max_level`.
- **Platform**: what differs between AWX, AAP and Ascender: the API root and the operator
  custom resource kinds (`platforms.py`).
- **Environment**: facts collected once per run, before any check runs. The controller
  contributes ping, config, scale counts and selected settings. The cluster contributes nodes,
  totals, storage, the custom resource, workloads, PVCs, ingresses and metrics-server usage.
  Collection is read-only and tolerant: whatever cannot be read (RBAC, no metrics-server) is
  listed under `errors`.
- **Check**: a function with a stable id (`category.name`), a level, requirements, optional
  dependencies and tags. It records metrics (value, unit, which direction is better), attributes
  and a verdict (pass, warn, fail, error, skip).
- **Report**: environment plus results plus run metadata (options, level, what was unavailable,
  cleanup problems). Every renderer and the comparison read only this.

## Decisions

### One canonical JSON report

All outputs are rendered from one document with a versioned [schema](../src/awx_tester/report.schema.json).
That keeps console, HTML and xUnit consistent, lets anyone re-render or compare old runs, and
makes "compare with others" a matter of exchanging files. The HTML report embeds the JSON, so
one file is enough to share.

### Metrics carry their direction

A metric says whether lower or higher is better, or that it is informational. The comparison
can then call a change *improved* or *regressed* without knowing anything about the check.
Single-sample timings are marked informational, because one request is not a benchmark.

### Comparison explains, not only judges

Next to check outcomes and metrics, a comparison lines up a flattened *fingerprint* of each
environment: platform and Kubernetes versions, node count and sizes, operator image, custom
resource spec, replicas, requests and limits, storage, data volume, settings. A slower run on a
smaller cluster, or with ten times the hosts, explains itself.

### Safety by default

- Read-only is the default level and the only one for targets given on the command line.
- Higher levels need agreement between the targets file (`max_level`) and the command line.
  `protected: true` overrides everything.
- Disruptive runs need the target name typed back.
- Mutating checks only touch objects they created. Names carry the run id, cleanup runs in a
  `finally` block, newest objects first, and problems are reported in the run section.
- Disruptive checks first verify that the component is healthy, so they do not kick an
  installation that is already down.
- Secret values in the custom resource spec are dropped. Credentials never enter a report.

### Why not Robot Framework

Robot Framework was evaluated because it brings an HTML log and report, `--xunit` output, and
readable keyword-driven tests. It does not cover the core needs here:

- **Comparison** across runs and clusters needs a typed result model (metrics with units and
  direction, environment fingerprints). Robot results are keyword logs with pass or fail; that
  model would have to be built next to it anyway.
- **Sizing and configuration reporting** is data collection, not test steps.
- **Performance measurement** (percentiles, concurrent workers, job timelines) is Python either
  way. Robot would only wrap it in keywords.
- xUnit and a self-contained HTML page take about 200 lines here. Adopting Robot would add a
  second language, a larger dependency, and two report styles to keep consistent.

If keyword-driven *acceptance* scenarios become a requirement later, a Robot library can sit on
top of the same `ApiClient` and `Report` without changing the report format.

### No numpy, no pandas

Percentiles over a few thousand samples do not need them. Fewer dependencies make installation
on jump hosts easier.

## Known limits

- One installation per run. To compare two, run twice and use `compare`.
- Load generation comes from one process, so very large installations may need several runners
  to saturate them.
- Event throughput uses ad hoc `ping` over local hosts. It measures the callback receiver and
  database path, not a realistic playbook mix.
- AAP 2.5+ gateway-specific features (authentication, Event-Driven Ansible, hub) are only
  recorded (gateway version), not tested.
