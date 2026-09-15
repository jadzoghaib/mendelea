> **This is the engineering record, kept whole.**
>
> It is the README this project had while it was being built, preserved because the
> reasoning in it is the actual asset: why the model is bitemporal, how the range-queried
> ingest works, what the policy-event correction cost to find, and which numbers were
> verified before being quoted. The [top-level README](../README.md) is now the
> business-facing front page; this is what sits behind it.
>
> Figures here were correct when written. Where the two disagree, the README is newer.

---

# Mendelea

An evidence timeline for reported genetic variants.

A laboratory signs out a variant, writes a report, and moves on. Years later
the evidence underneath that report has changed — ClinVar submissions
accumulate, review status strengthens, classifications flip — and nothing
tells them. Mendelea reconstructs what the evidence said on any past date and
reports what has moved since.

It reports that the **evidence** moved. It never asserts a classification.

> Research use only. Not a medical device. Not validated for clinical use.

---

## The idea in one query

Everything in this repository exists so that the product is six lines of SQL
rather than a research project:

```sql
SELECT cv.case_ref, cv.allele_id,
       then_.bucket AS at_signout,
       now_.bucket  AS current
FROM   case_variant cv
JOIN   assertion_span then_ ON then_.allele_id = cv.allele_id
       AND cv.reported_on BETWEEN then_.valid_from AND then_.valid_to
JOIN   assertion_span now_  ON now_.allele_id = cv.allele_id AND now_.is_current
WHERE  then_.bucket IS DISTINCT FROM now_.bucket;
```

If the data model is right, the feature is trivial. If it is wrong, the
feature is impossible. That is the whole architectural argument.

---

## Architecture

### Bitemporal, not "current state"

Two independent time axes: **evidence time** (when did ClinVar assert this?)
and **decision time** (when did the laboratory sign it out?).

Snapshots are immutable and content-addressed. Nothing is ever rebuilt in
place — a pipeline that overwrites current state destroys the past on every
run and makes reanalysis impossible by construction.

```
evidence/snapshots/clinvar/2019-06-03/spike.parquet   + .manifest.json (sha256, source URL, retrieval time, git sha)
evidence/snapshots/clinvar/2022-06-02/spike.parquet
evidence/snapshots/clinvar/2026-08-06/spike.parquet
```

`assertion_span` is derived from the accumulation: one row per allele per
unbroken run of identical classification, with the dates it was valid
between. Absence is an explicit state, so a retraction from ClinVar shows up
as a transition rather than a silent gap.

### Three planes

| Plane | Contents | Size | Shared? |
|---|---|---|---|
| `evidence/` | Snapshots, assertion spans | tens of GB | **shared by every tenant** |
| `cases/` | One lab's reported variants | ~5 MB per lab | isolated |
| `decisions/` | Append-only review ledger | ~1 MB per lab | isolated |

The expensive plane is public and shared; the private plane is tiny. That
asymmetry is what lets modest infrastructure serve many tenants.

**The case plane never receives PHI.** Coordinates, what was reported, and
when — nothing else. `case_ref` is the laboratory's own pseudonymous key. This
is a design constraint, not an MVP shortcut: it keeps GDPR Article 9 out of
scope and shrinks the security review to something a small team can answer.

### Cheap ingest

ClinVar publishes a tabix index beside every archived release, and NCBI
honours HTTP range requests. So a gene is fetched by pulling only the
compressed blocks that contain it:

```
BRCA1, release 2019-06-03:  0.36 MB fetched from a 19.1 MB file  (98.1% saved)
```

`bgzf.py` and `tabix.py` implement this in pure Python — no pysam, which does
not build cleanly on Windows.

### Three dependencies, and why not four

`duckdb`, `requests`, and nothing else at runtime. DuckDB already read every
snapshot in place; it now writes them too, which retired `pyarrow` — by an
order of magnitude the largest thing the project installed. That was not
housekeeping: pyarrow ships an unsigned native library, and Windows Smart App
Control blocked it outright on the development machine, taking the whole test
suite down at collection time. The README warned that every dependency is one
more thing that can fail on someone else's laptop. It failed on ours.

Rows reach Parquet through a JSON-lines spill file rather than parameter
binding, because DuckDB's `executemany` costs about 10 ms per row — five
minutes for a 30,000-variant gene, against 0.3 s for the same rows via
`read_json`. Column types are declared, not inferred, so an all-NULL column in
a small snapshot cannot come back as something else, and the write is still
byte-identical run to run, which is what the manifest's sha256 depends on.

---

## Setup

```powershell
python -m venv "$env:USERPROFILE\.venvs\mendelea"
& "$env:USERPROFILE\.venvs\mendelea\Scripts\python.exe" -m pip install -e ".[dev]"
```

Two deliberate placements:

- **Data lives outside OneDrive**, at `~/mendelea-data` (override with
  `MENDELEA_DATA_DIR`). The evidence plane grows to tens of gigabytes; syncing
  that would compete with your quota on every rebuild.
- **The virtualenv lives outside the project**, for the same reason.

---

## Commands

```powershell
mendelea ingest --panel spike --from 2019 --to 2026 --per-year 1
mendelea spans  --panel spike
mendelea spike  --panel spike            # the Phase 0 gate
mendelea timeline --gene BRCA1 --on 2019-06-03
mendelea provenance --panel spike        # integrity + reproducibility audit
mendelea serve --port 8000               # the time machine
```

`ingest` is idempotent: existing snapshots are skipped, never rewritten. It
takes an advisory lock per panel — a second concurrent run is refused rather
than allowed to double the load on NCBI and halve everyone's bandwidth. Pass
`--force` to steal a lock from a run you know is dead.

## The time machine (Phase 2)

`mendelea serve` opens a read-only view on the timeline. Pick a gene, scrub
across ingested releases, and everything re-renders as the evidence stood on
that date — with what each variant is classified as *today* beside it.

The headline it exists to deliver:

> On **2018-12-25**, **518** TP53 variants were classified *uncertain*.
> **102** of those — **19.7%** — are today classified pathogenic or benign.

**The scrubber is the chart.** A bare slider gave a date and nothing else, so
the reader had to move it to discover where anything happened. It is now a
stacked column per release showing how the gene's classifications were
distributed, which makes the shape of the change visible before a single
click: the uncertain band swelling, the conflicting band appearing, the corpus
growing underneath. The x axis is proportional to time, so a year of silence
looks like a year. Clicking or dragging anywhere on it selects a release;
so do the arrow keys, `Home` and `End`.

Classification colours are a diverging scale — a pathogenic arm and a benign
arm, two steps each, uncertain as the neutral midpoint — validated for
protan, deutan and tritan separation against the panel surface rather than
picked by eye. `OTHER` is hatched instead of coloured: a second grey cannot be
told from the first, and a seventh hue would compete with a scale it is not
part of.

**The panel's own rate sits under the gene's.** The first question a
laboratory asks is about its whole back catalogue, and that number lived only
in the CLI, which is no use in a meeting. It is now on screen beside the
per-gene headline — *of the 28,859 variants uncertain in 2018, 4.6% are now
pathogenic or benign* — counted over the same timeline `mendelea spike`
counts, so the demo and the command line cannot disagree in front of a
customer.

**The gene picker ranks by the rate the product reports.** It ranked by how
many variants had ever held two classifications, which is precisely the
movement the rest of the system is at pains not to quote: it counts
`UNCERTAIN → CONFLICTING`, so the list opened on BRCA2 at 28% beside a
headline of 4.6%, and put RAD51C sixth at 22% where 0.3% is actionable. A
picker that promises what the view does not deliver is worse than no picker.
Each entry now states the rate and the count it came from, and genes with
fewer uncertain variants at baseline than the README quotes rates for sort
below the ones whose numbers carry weight — still selectable, never leading.

**The detected policy event is drawn, not just described.** The release step
the sweep landed on is shaded and marked, and any row whose movement matches
it is flagged `! relabelled` and sorted below genuine movement. A reader can
see at a glance which part of the jump is ClinVar changing a rule.

**Every count is the population, never the page.** The table pages at 300
rows and says how many rows exist; showing 400 of BRCA2's 9,708 with nothing
to say so was the previous behaviour. `moved only` and a search over
variation ID, position or condition narrow the whole result, not the page.

**Any view is a link.** Gene, release, filters and the open variant live in
the URL fragment, so a specific finding can be pasted into an email and it
opens on that variant's history. Clicking a row opens a detail pane with the
variant's full span history as a track plus the raw ClinVar term behind each
bucket, and the table sheds its least essential columns when the pane leaves
it less room.

Stdlib only, no build step, no external assets. That is deliberate: this is
what goes in front of a laboratory in a first meeting, and every dependency is
one more thing that can fail on someone else's laptop. The query layer
(`web/queries.py`) is separate from transport, so moving to FastAPI is an
adapter rather than a rewrite.

The gene ranking and the policy scan are computed once per process, not per
request: DuckDB admits one writer or many readers to a file and never both, so
a rebuild waits for the server to stop and a restart is the only invalidation
needed. Nothing is cached in the browser — a demo that serves a stale page
after an upgrade is worse than a slow one, and Chrome will do exactly that
given keep-alive and a `Content-Length`.

Read-only, public ClinVar data only. `/api/case/report` is the one
authenticated endpoint and the only place tenant data is reachable; the tenant
comes from the token, never from the request.

Reclassification rates by gene, 2018→2025, from the running demo:

```
BRCA2 28.2%   MSH2 25.4%   VHL  23.3%   ...   CHEK2 12.8%
TP53  28.0%   BRCA1 25.0%  STK11 22.8%        NF1   10.6%   EPCAM 3.4%
```

---

## Phase gates

| Phase | Deliverable | Gate |
|---|---|---|
| **0** | Movement measured on public data, no customer needed | actionable movement ≥ 2% |
| **1** | Evidence timeline over ~90 releases | point-in-time query correct on 20 hand-checked variants |
| **2** | Time-machine demo, public | 10 laboratory conversations booked |
| **3** | Case plane, tenant isolation, server-side ledger | first paid retrospective audit delivered |

Do not pass a gate on optimism.

---

## Phase 0 result

Baseline 2018-12-25, current 2025-12-28. **Gate passes on both panels.**

| panel | genes | VUS at baseline | → P/LP | → B/LB | actionable |
|---|---|---|---|---|---|
| spike | 5 | 8,560 | 208 | 411 | **7.2%** |
| hereditary-cancer | 31 | 28,859 | 518 | 809 | **4.6%** |

Both verified before being quoted: recomputed directly from the Parquet files
bypassing all span logic (identical), and four moved variants checked against
live ClinVar via NCBI eutils (exact match, all reviewed by expert panel).

Ingest moved 61 MB against 1.4 GB of whole-file downloads — 23×. The 31-gene
timeline is 427,933 spans over 198,341 alleles.

### Movement is concentrated, not uniform

Actionable rate by gene (2018→2025, genes with >200 VUS at baseline):

```
TP53   19.7%      MLH1    8.4%      MSH2   6.9%      MUTYH  4.6%
PTEN   10.2%      BRCA1   8.4%      NF1    6.3%      BRCA2  4.6%
MEN1    8.5%      CDH1    7.1%      VHL    5.3%      POLE   4.9%
```

TP53 moves four times as often as BRCA2. The 5-gene panel scores higher (7.2%)
than the 31-gene panel (4.6%) precisely because it is made of heavily-studied
genes. Any claim about "how much moves" is meaningless without naming the gene
set it was measured on.

### The policy event, and why the tool now detects them

Raw movement looked like 67.1%, which was wrong. Nearly all of it was one
transition:

```
2023-02-26 -> 2023-04-30 :  UNCERTAIN -> CONFLICTING  5,843 variants (13.3% of corpus)
```

Across that nine-week window VUS fell 15,651 → 9,823 while conflicting rose
3,225 → 9,080 — a one-to-one swap while the corpus barely grew. That is the
database changing how it aggregates, not laboratories revising variants.
ClinVar documents a change to its conflict-reporting rules effective June 2022
(<https://www.ncbi.nlm.nih.gov/clinvar/docs/clinsig/>); the variant-level
aggregate in the VCF appears to have been recomputed in early 2023. The
documented rule is the probable cause, but the date is empirical, not quoted.

A separate, later event — the rename of `Conflicting_interpretations_of_pathogenicity`
to `Conflicting_classifications_of_pathogenicity` between the 2023 and 2024
releases — is absorbed by bucketing and moved no counts at all (9,325 → 9,902).

`reports/policy.py` now detects these automatically: any single release step
in which one transition sweeps more than 5% of the corpus is flagged. Real
steps in this corpus run 1–3%. Flagged movement is reported separately rather
than discarded, so the adjustment stays visible:

```
movement rate, policy-adjusted   36.8%   (raw 67.1%)
ACTIONABLE movement rate          7.2%   (excludes CONFLICTING entirely,
                                          so policy events cannot inflate it)
```

**Quote 7.2%, never 67%.** Telling a laboratory that 5,843 of its variants
moved when a rule changed would send it re-reviewing thousands of cases for
nothing — the fastest possible way to lose the customer.

#### The adjusted figure was itself wrong, in the other direction

It read 8.8% until the suspect rule was corrected. A movement counted as
policy-suspect if its *transition* matched a detected event, which flagged
every UNCERTAIN → CONFLICTING move the timeline has ever held — 4,995 of them
— when the sweep accounts for 2,596. The rest are ordinary: a submitter
disagrees, a variant becomes conflicted, at a release step nowhere near the
event. Subtracting those understated real movement by a factor of four.

A movement is now suspect only if the transition matches an event *and* the
variant's current state began at that event's release step. The same
correction moves the 31-gene panel from 5.3% to 26.7%. Neither number is the
quotable one and that is the point: the adjusted rate is still dominated by
movement into CONFLICTING, which is real but tells a laboratory nothing it can
act on. **The actionable rate does not move at all** — 7.2% and 4.6% before
and after — because it excludes CONFLICTING by construction. A metric that
survives a bug in the adjustment beside it is the one to build a business on.

## The case plane (Phase 3)

```powershell
mendelea case-demo   --out demo-cases.csv --count 800   # synthetic lab export
mendelea case-load   --tenant demo-lab --file demo-cases.csv
mendelea case-report --tenant demo-lab --out findings.json

mendelea case-review --tenant demo-lab --case-ref CASE-00771 \
                     --allele-id <id> --verdict RECONTACT \
                     --rationale "now pathogenic, 3-star" --reviewer curator@lab
mendelea case-log    --tenant demo-lab                  # the ledger
mendelea case-verify --tenant demo-lab                  # chain integrity
```

Each review is stamped with the evidence snapshot in view at the time, so the
ledger records not only what was decided but what it was decided against.

A laboratory supplies coordinates, what it reported, and when. Nothing else.
No genome, no phenotype, no identifier. `case_ref` is their own pseudonymous
key and we never learn what it points to.

### Reconciliation comes before findings

The movement query is the easy half. What decides whether the report is
trustworthy is the accounting of what could *not* be answered, because there
are two ways to fail silently and both look exactly like "nothing changed":

- the variant is **not in the evidence plane** — wrong panel, never in
  ClinVar, a coordinate that did not normalise;
- the variant is there, but was **signed out before our earliest snapshot**,
  so we cannot say what the evidence said at the time.

Every input row therefore lands in exactly one bucket, and the reconciliation
is printed *before* any finding. Below a 90% match rate the report says so
explicitly rather than letting the movement figures be read as representative.

```
RECONCILIATION   (every input row is accounted for)
  variants loaded                      800
  not found in evidence                  0
  signed out before coverage            38
  examined                             762    95.3%
```

A report that quietly drops what it could not examine tells a customer their
back catalogue is clean when in truth it was never looked at.

### Left-alignment, and why it had to come first

ClinVar left-aligns its records; a laboratory export carries no such
guarantee. Inside a repeat the same deletion is writable at several positions,
all valid VCF:

```
reference  ... G T T T T T C ...
                 ^ ^ ^ ^ ^      "delete a T" — five correct spellings
```

An unaligned indel hashes to an identifier that matches nothing. It does not
error; it simply drops out of the report. `evidence/reference.py` fetches the
reference for each gene region (a few MB for the whole panel, the same
fetch-the-region trick the ingest uses) and rolls indels left before hashing.

Verified as a safe migration on real data: recomputing identifiers for
**30,574 ClinVar variants including 6,546 indels changed exactly none of
them** — confirming ClinVar is already left-aligned, that the algorithm is
idempotent on real input, and that no re-ingest was needed.

If the reference cannot be fetched the load warns loudly and continues with
trim-only. Degrading silently would reintroduce precisely the join failures
this exists to prevent.

## Tenants and isolation

```powershell
mendelea tenant-create --tenant lab-a --name "Laboratory A" --label laptop
mendelea tenant-list
mendelea tenant-revoke --token-id <id>
```

Tokens are `mdl.<token_id>.<secret>`. The `token_id` is public so a token can
be listed and revoked without knowing the secret; only the SHA-256 of the
secret is stored, so a database dump yields no working credential. The
plaintext is printed once and is not recoverable.

`/api/case/report` is the only endpoint that touches tenant data. **The tenant
is taken from the token and never from the request** — otherwise a valid token
for one laboratory could be aimed at another's data, which is authentication
without authorisation. Verified live: a rival token sent with
`?tenant=demo-lab` still returns its own empty result.

The time machine stays public. It serves ClinVar, which is public.

### Isolation is enforced, not merely intended

Authentication is the smaller half. The defect that actually occurred was a
query over `case_variant` with no tenant predicate whose docstring claimed to
serve one tenant — a perfectly authenticated caller would have received every
laboratory's variants. So `tests/test_tenant_isolation.py` walks the AST of
every source file and fails the build if any SQL string naming a tenant-owned
table omits `tenant_id`:

```
FAILED  SQL touching tenant-owned tables without a tenant_id reference:
          reports/movement.py:165 -> SELECT cv.case_ref ... FROM case_variant cv
```

That check generalises in a way tests do not: it catches queries nobody
thought to call. It is also why the INSERTs name their columns explicitly
rather than relying on positional order.

## Known limitations

These are real and should be read before quoting any number this produces.

- **Identifiers are not VRS-conformant.** The digest construction
  (sha512, truncated to 24 bytes, base64url) is GA4GH's, but the serialisation
  is not: true VRS resolves the reference sequence through SeqRepo, a
  multi-gigabyte dependency deferred to Phase 1.5. Identifiers are namespaced
  `mendelea:VA.` and **must not be published as VRS IDs**. What is claimed is
  stability, not federation.
- **Normalisation is reference-free.** Shared prefixes and suffixes are
  trimmed, which makes padded representations of the same indel agree. It does
  *not* left-align across repeat regions — that needs the reference sequence.
  Sufficient for ClinVar-to-ClinVar comparison, since ClinVar publishes
  pre-normalised records; a genuine limitation when matching a customer's own
  file in Phase 3.
- **Terminology drift is absorbed, not eliminated.** ClinVar renamed
  `Conflicting_interpretations_of_pathogenicity` to
  `Conflicting_classifications_of_pathogenicity` mid-window. Comparing raw
  strings would read that rename as every conflicted variant moving on one
  date. Buckets exist to absorb this, and `test_clinvar.py` guards it — but
  the next rename will need the same treatment.
- **Policy-event detection is sensitive to snapshot density.** The same event
  registered at 13.3% of corpus against bimonthly snapshots but only 5.3%
  against annual ones, because a coarse step dilutes the sweep across a year
  of ordinary movement. At annual density it cleared the 5% threshold only
  barely; a slightly larger corpus or slightly smaller event would have gone
  undetected. Ingest densely around any suspected event before trusting a
  negative result, and treat the threshold as panel-specific rather than
  universal. Density now bounds the *attribution* too, not just the
  detection: the suspect key is the event's release date, so at annual
  density the "event" spans 2022-12-24 → 2023-12-30 and a year of ordinary
  drift observed at that snapshot is subtracted along with the sweep. The
  31-gene panel's 26.7% is therefore a floor, and the spike panel's 36.8%,
  measured against a nine-week window, is the better-resolved figure.
  Densifying the 31-gene ingest across early 2023 is the fix.
- **Policy attribution catches sweeps, not gradual policy effects.** Only a
  transition large enough to trip the threshold in one release step is
  flagged. ClinVar's conflict rule changed effective June 2022 and the
  variant-level aggregate was recomputed in a detectable burst, but any part
  of it that trickled across other releases is counted as ordinary movement.
  This is the main reason the policy-adjusted rate is worth less than the
  actionable one.
- **The suspect key is a near-enough proxy, measured as such.** Exactly
  "did this allele participate in the sweep" needs `assertion_dense` and a
  join per allele. Keying on the date the current span opened answers it to
  within 18 alleles of 3,850 on the 31-gene panel — 0.5%, and identical to one
  decimal place in the reported rate. Stated in `policy.suspect_keys` too,
  since that is where someone will look.
- **Absence is only meaningful within a panel.** Spans are built per panel
  over identical gene regions. Change the gene list and `ABSENT` transitions
  become artefacts, which is why spans are never built across panels.
- **The decision ledger is tamper-evident, not tamper-proof.** It detects
  edits made outside `append()`. An actor who can rewrite the whole table can
  recompute the whole chain. Per-entry signing is Phase 4.

---

## Tests

```powershell
pytest -q
```

262 tests, offline by default. The ones worth reading first are `test_spans.py`
(the bitemporal invariants — every headline number is a query over that table)
and the drift guard at the top of `test_clinvar.py`.

Two of them exist because of this round's bugs, and both are the kind a green
suite happily hides: a movement flagged as a relabelling when the same
transition happened at an unrelated release, and a page of 300 rows reported
as though it were the whole population. `test_snapshot.py` covers the Parquet
round trip column by column, including the empty snapshot and the case where
a failed write must leave nothing behind.
