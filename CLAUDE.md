# Mendelea — product context

An evidence timeline for reported genetic variants. Laboratories learn which of
their signed-out results the evidence no longer supports. **It reports that the
evidence moved; it never asserts a classification.** That distinction is the
regulatory position, not a stylistic one.

## Architecture, and why

**Bitemporal, not current-state.** Two time axes: evidence time (when did ClinVar
assert this) and decision time (when did the lab sign it out). Snapshots are
immutable and content-addressed; `assertion_span` is a slowly-changing dimension
derived from the accumulation. A pipeline that overwrites current state destroys
the past on every run and makes reanalysis impossible by construction.

**Three planes, strictly separate.** `evidence/` is public, shared by every
tenant, large. `cases/` is one lab's reported variants — tiny, pseudonymous,
**never PHI by design** (that constraint keeps GDPR Art. 9 out of scope, which is
the only reason a solo founder can ship this). `decisions/` is an append-only
hash-chained ledger.

**Range-queried ingest.** Pure-Python BGZF + tabix readers fetch one gene from a
193 MB ClinVar release over HTTP range requests — 98% saved. No pysam; it does
not build on Windows.

**Two runtime dependencies: `duckdb` and `requests`.** DuckDB writes the
snapshots as well as reading them. Do not reintroduce pyarrow — its unsigned
native library is blocked by Windows Smart App Control on this machine and took
the entire suite down at collection time. `snapshot.write_snapshot` spills rows
to JSON lines and loads them with `read_json`; binding them one by one costs
~10 ms per row, which is five minutes for a single large gene.

## Things that cost real effort to learn

- **AlphaMissense predictions are CC BY 4.0**, relicensed March 2024. The
  non-commercial restriction is gone. A third-party derivative dataset
  (Zenodo 10255502) is still NC-SA — check which artifact you ingest.
- **ClinVar re-aggregated in early 2023**, relabelling 6,107 variants
  UNCERTAIN → CONFLICTING in one nine-week window. Naive reading gives 40%
  movement instead of 4.6%. `reports/policy.py` detects this class of event.
  **Quote 4.6% actionable, never 40% total.**
- **Policy-suspect is keyed on (from_bucket, to_bucket, event_date)**, via
  `policy.suspect_keys` — the one definition, read by the Phase 0 gate, the
  case report and the web layer alike. Matching the bucket pair alone flags
  every such transition ever recorded, not just the sweep, and understated
  real movement fourfold (spike panel: 8.8% reported against 36.8% actual;
  31-gene: 5.3% against 26.7%). The actionable rate is immune either way,
  which is why it is the quotable one.
- **December ClinVar releases are filed under the following year's directory.**
- **Identifiers are `mendelea:VA.`, NOT VRS-conformant.** Digest construction is
  GA4GH's; serialisation is not (real VRS needs SeqRepo, ~15 GB). Never publish
  these as VRS IDs.
- Movement is panel-dependent: TP53 19.7%, BRCA2 4.6%, EPCAM 3.4%. Any claim
  about "how much moves" is meaningless without naming the gene set.

## Working rules

- Tenant isolation is enforced by `tests/test_tenant_isolation.py`, which walks
  the AST and fails if any SQL naming a tenant-owned table omits `tenant_id`.
  Keep statements in one string literal — split literals defeat the check.
- The tenant comes from the token, never from the request.
- `spans.loaded_panel()` / `spans.coverage()` are the only place to ask which
  panel is loaded. `snapshot_manifest` spans every panel ever ingested, and
  reading it unfiltered has caused the same bug four times.

## Honest state

237 tests pass. Every substantive bug this project has had was found by reading
code or running it on real data — never by the test suite, which was green
throughout. Budget for reading, not just testing.

Still true of the latest round: the policy-suspect error, the page-as-population
error, and a CSS comment that silently swallowed the rule after it were all
found by running the thing and reading the output, with 199 tests green. The
browser is part of the test surface for `web/`; screenshots alone would have
missed all three.

No TLS, no rate limiting: bind to 127.0.0.1 only.
