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

`mendelea serve` opens a read-only view on the timeline. Pick a gene, drag the
slider across ingested releases, and the table re-renders as the evidence
stood on that date — with what each variant is classified as *today* beside
it.

The headline it exists to deliver:

> On **2018-12-25**, **518** TP53 variants were classified *uncertain*.
> **102** of those — **19.7%** — are today classified pathogenic or benign.

Stdlib only, no build step, no external assets. That is deliberate: this is
what goes in front of a laboratory in a first meeting, and every dependency is
one more thing that can fail on someone else's laptop. The query layer
(`web/queries.py`) is separate from transport, so moving to FastAPI is an
adapter rather than a rewrite.

Read-only, public ClinVar data only. The case and decision planes are not
reachable from it — serving those needs auth and tenant isolation (Phase 3).

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
movement rate, policy-adjusted    8.8%   (raw 67.1%)
ACTIONABLE movement rate          7.2%   (excludes CONFLICTING entirely,
                                          so policy events cannot inflate it)
```

**Quote 7.2%, never 67%.** Telling a laboratory that 5,843 of its variants
moved when a rule changed would send it re-reviewing thousands of cases for
nothing — the fastest possible way to lose the customer.

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
  universal.
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

The suite is offline by default. The tests worth reading first are
`test_spans.py` (the bitemporal invariants — every headline number is a query
over that table) and the drift guard at the top of `test_clinvar.py`.
