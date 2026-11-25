# awx-tester

Test, benchmark and describe an **AWX**, **Ansible Automation Platform (AAP)** or **Ascender**
installation on Kubernetes, and get a standardized report that can be compared with other
clusters or with earlier runs on the same cluster.

```text
$ awx-tester run -t lab
14 checks at level readonly against lab (awx, https://awx.lab.example.com)
PASS  platform.ping                         0.03s  latency_ms=32.9 ms  instances=1
PASS  platform.instances                    0.37s  instances=1  capacity_total=157 forks  cpu_total=4 cores
WARN  cluster.nodes                         0.00s  nodes=1  cpu_allocatable=4 cores  memory_allocatable_gib=15.4 GiB
       node k3s has only 4.69 GiB ephemeral storage (logs and emptyDirs); below 10 GiB
PASS  perf.api_latency                     54.20s  ping_p95_ms=28.5 ms  me_p95_ms=370 ms  config_p95_ms=355 ms
...
Overall: WARN   13 pass, 1 warn  in 69.5s
wrote reports/lab/20261125182911-6a1f/report.json
wrote reports/lab/20261125182911-6a1f/report.html
wrote reports/lab/20261125182911-6a1f/junit.xml
```

## What it answers

- **Does it work?** Functional checks of the API, instances, capacity, execution environments,
  ad hoc commands, project syncs, job templates, workflows and schedules.
- **How fast is it?** API latency and throughput, job queue and run times, jobs per minute,
  job event processing rate, and recovery time after losing a pod.
- **What is it running on?** Cluster sizing and configuration: nodes, CPU, memory, ephemeral
  storage, distribution, storage classes, operator version, the custom resource spec, replicas,
  resource requests and limits, data volume and performance-related settings.
- **What changed?** `awx-tester compare` lines up any number of reports and shows which checks,
  metrics and configuration values moved, with noise filtering.

## Install

```bash
pipx install git+https://github.com/jvzantvoort/awx-tester
# or, from a checkout
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Python 3.10 or newer. Kubernetes access uses your kubeconfig. Without it, the cluster checks are
skipped and the API checks still run.

## Quick start

1. Describe your installations in `awx-tester.yml` (see [examples/targets.example.yml](examples/targets.example.yml)):

   ```yaml
   targets:
     lab:
       type: awx                    # awx | aap | ascender
       url: https://awx.lab.example.com
       auth:
         username: admin
         password_secret: {name: awx-admin-password, key: password}   # or password_env / token_env
       kubernetes: {context: lab, namespace: awx}
       max_level: mutating          # how far awx-tester may go on this target
     production:
       type: aap
       url: https://aap.example.com
       auth: {token_env: AAP_PROD_TOKEN}
       kubernetes: {context: prod, namespace: aap}
       protected: true              # never anything but read-only
   ```

2. Run it:

   ```bash
   awx-tester targets                         # what is configured
   awx-tester list-checks                     # what can be checked
   awx-tester run -t lab                      # read-only (the default)
   awx-tester run -t lab -l mutating          # also create objects and run jobs
   awx-tester run -t lab -l disruptive --confirm lab   # also delete pods and measure recovery
   ```

3. Compare:

   ```bash
   awx-tester compare reports/lab/<before>/report.json reports/lab/<after>/report.json --html diff.html
   awx-tester compare lab.json staging.json prod.json      # first one is the baseline
   ```

You can also run without a targets file, in read-only mode only:

```bash
AWX_PW=... awx-tester run --type ascender --url https://ascender.example.com \
  --username admin --password-env AWX_PW --kube-context lab --namespace ascender
```

## Test levels: destructive or not

| Level | What it does | Safe on production? |
|---|---|---|
| `readonly` (default) | API GETs and Kubernetes list/get only. Adds some request load (`perf.api_concurrency`). | Yes |
| `mutating` | Creates its own organization, inventories, credential, project, templates and schedule (all named `awx-tester-<run id>-…`), runs jobs against `localhost` in the execution environment, then deletes everything. Adds entries to job history and the activity stream. | Only in a maintenance window |
| `disruptive` | Also deletes web, task and managed PostgreSQL pods and measures downtime and recovery. | No |

Two independent switches must agree before anything beyond read-only runs:

1. The target's `max_level` in the targets file. It defaults to `readonly`, and `protected: true` pins it there.
2. The `--level` on the command line.

Disruptive runs also need the target name typed back (`--confirm NAME` or at the prompt).
Targets given only on the command line are always read-only.

## Outputs

Every run writes `report.json`, the canonical result, plus the formats you ask for with `-f`:

| Format | File | Use |
|---|---|---|
| `console` | – | Live progress and a summary |
| `json` | `report.json` | The standardized report ([schema](src/awx_tester/report.schema.json), [format](docs/report-format.md)) |
| `html` | `report.html` | Self-contained page for people. It embeds the JSON, so `compare` accepts it too |
| `junit` | `junit.xml` | xUnit for CI systems, one suite per category, metrics as properties |

Use `--redact` before sharing a report outside your organization. It replaces hostnames, node
names, ingress hosts, the URL, kube contexts and IP addresses, and keeps sizing, versions and
measurements. `awx-tester render report.json --redact --json shared.json` does the same for a
saved report.

Exit codes: `0` when everything passed or warned, `1` when a check failed or errored (and for
`compare --fail-on-regression`), `2` for usage, configuration or safety refusals.

## Checks

`awx-tester list-checks --markdown` prints the current list. In short:

| Category | Checks |
|---|---|
| `platform` (readonly) | ping, auth, instances, instance groups, execution environments, recent job outcomes and queue times, Prometheus metrics |
| `cluster` (readonly) | node readiness and sizing, storage, operator/custom resource health, pods and restarts, resource requests and live usage |
| `perf` (readonly) | API read latency per endpoint, API throughput under concurrency |
| `func` (mutating) | workspace, CRUD latency, ad hoc command, project sync, job template, workflow, schedule punctuality |
| `perf` (mutating) | concurrent job throughput with a per-job timeline, job event processing throughput |
| `chaos` (disruptive) | web pod loss, task pod loss, a running job during task pod loss, managed database loss |

Select with `--only` and `--skip`: a check id or category glob, `tag:NAME` or `level:NAME`, e.g.
`--only 'perf.*' --skip tag:slow`. Dependencies are pulled in automatically.

Tune with `options:` in the targets file or `--set section.key=value`, e.g.
`--set api_latency.iterations=50 --set job_throughput.jobs=25`. All defaults are listed in
[`context.py`](src/awx_tester/context.py). The effective options are stored in each report, and
`compare` warns when two runs used different ones.

## Making runs comparable

- Use the same awx-tester version and options. `compare` warns otherwise.
- Prefer token authentication. With basic auth, every API request pays for password hashing
  (often 300 ms or more), and that cost swamps the latency you want to measure.
- Data volume matters: the report records object counts (`scale.*`) so you can see whether two
  installations are alike.
- Repeat disruptive checks with `--repeat N`, for example `-l disruptive --confirm lab --repeat 3`.
  Each check then disrupts and recovers N times (waiting `chaos.settle_s` seconds between
  repetitions). Each metric reports the median under its usual name, adds `<metric>_max` for the
  worst repetition, and keeps every value in the `samples` attribute. The series stops at the
  first failed repetition.
- Single runs are noisy. `compare` ignores changes under `--threshold` (10 % by default) and
  under a per-unit noise floor (5 ms, 0.5 s, 1 %).
- Label runs (`--label change=CHG123 --notes "after operator upgrade"`) so the history explains
  itself.

## Platform notes

| Type | API root | Custom resource |
|---|---|---|
| `awx` | `/api/v2/` | `AWX` (`awx.ansible.com`) |
| `ascender` | `/api/v2/` | `Ascender` (`ascender.ansible.com`), else `AWX` (`awx.ansible.com`) |
| `aap` | `/api/controller/v2/` (2.5+, behind the gateway), else `/api/v2/` (2.4) | `AnsibleAutomationPlatform`, else `AutomationController` |

On AAP 2.5 and later the gateway may own organizations. If creating one is refused, set
`workspace.organization` to an existing organization for mutating runs.

Air-gapped? Set `project.scm_url` to an internal mirror of
[ansible-tower-samples](https://github.com/ansible/ansible-tower-samples), or to `""` to skip
the project, job template, workflow and schedule checks.

## Why not Robot Framework?

Robot Framework was considered, and its HTML log and `--xunit` output are good. It was left out
because the core of this tool is a *comparable data model*: typed metrics with a direction,
environment fingerprints and cross-run comparison. Robot's reports are per run and keyword
oriented, so the model, the comparison and the sizing report would have to be written anyway,
plus a second language and dependency for contributors. See [docs/design.md](docs/design.md).

## Documentation

- [docs/design.md](docs/design.md): architecture and design decisions
- [docs/report-format.md](docs/report-format.md): the report, its stability rules and how comparisons work
- [docs/writing-checks.md](docs/writing-checks.md): adding a check

## License

Apache-2.0. See [LICENSE](LICENSE).
