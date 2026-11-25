# Changelog

Notable changes per release. Reports made by different versions can be
compared; `awx-tester compare` warns when the versions differ.

## 0.2.0 (2026-11-25)

### Added

- `--repeat N` (option `chaos.repeat`) for the disruptive checks. Each check
  disrupts and recovers N times, waiting `chaos.settle_s` seconds between
  repetitions. Metrics report the median under their usual names, a
  `<metric>_max` metric records the worst repetition, and the `samples`
  attribute keeps every value.

### Fixed

- The disruptive checks undercounted API downtime when a health probe hung
  until its timeout, and missed an outage that started immediately. Web pod
  downtime measured with 0.1.0 can be several times too low, so compare it
  with care.

## 0.1.0 (2026-11-25)

First release: readonly, mutating and disruptive checks for AWX, AAP and
Ascender; cluster sizing and configuration fingerprint; console, HTML, xUnit
and JSON reports; `compare` and `--redact`.
