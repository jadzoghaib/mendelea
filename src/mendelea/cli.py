"""Command line entry point.

    mendelea ingest --panel spike --from 2019 --to 2026 --per-year 2
    mendelea spans  --panel spike
    mendelea spike  --panel spike
    mendelea timeline --gene BRCA1 --on 2019-06-03
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import config as config_module
from . import locks
from . import tenancy
from .cases import model
from .cases import report as case_report
from .db import connect
from .decisions import ledger
from .evidence import clinvar, genes, snapshot, spans
from .reports import movement, policy

PANEL_DIR = config_module.panel_dir()


def _resolve_panel(name: str) -> Path:
    candidate = Path(name)
    if candidate.exists():
        return candidate
    candidate = PANEL_DIR / f"{name}.json"
    if candidate.exists():
        return candidate
    raise SystemExit(f"no panel named {name!r} in {PANEL_DIR}")


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_ingest(args) -> int:
    cfg = config_module.load()
    panel_path = _resolve_panel(args.panel)
    panel_slug, gene_symbols = genes.load_panel(panel_path)

    # One ingest per panel at a time. Concurrent runs do not corrupt anything,
    # but they double the load on NCBI and make both crawl.
    lock_path = cfg.data_dir / f".ingest-{panel_slug}.lock"
    try:
        with locks.exclusive(lock_path, force=args.force):
            return _ingest(args, cfg, panel_slug, gene_symbols)
    except locks.LockHeld as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1


def _ingest(args, cfg, panel_slug, gene_symbols) -> int:

    resolver = genes.GeneResolver(
        cache_path=cfg.reference_dir / "gene_regions.json",
        bundled=PANEL_DIR / "gene_regions.json",
        assembly=cfg.assembly,
    )
    print(f"resolving {len(gene_symbols)} genes ...", flush=True)
    regions = resolver.resolve_all(gene_symbols)

    releases = clinvar.select_releases(args.start, args.end, per_year=args.per_year)
    if not releases:
        print("no releases found in that range", file=sys.stderr)
        return 1

    print(f"{len(releases)} releases: {releases[0]} .. {releases[-1]}")

    fetched = 0
    written = 0
    warned: set[str] = set()
    for index, release in enumerate(releases, start=1):
        try:
            result = snapshot.ingest_release(
                release=release,
                regions=regions,
                panel_slug=panel_slug,
                snapshot_root=cfg.snapshot_dir,
                cache_dir=cfg.cache_dir,
                assembly=cfg.assembly,
            )
        except Exception as exc:  # noqa: BLE001 - one bad release must not kill the run
            print(f"  [{index}/{len(releases)}] {release}  FAILED: {exc}", flush=True)
            continue

        manifest, did_write = result.manifest, result.written
        # Region warnings repeat on every release; surface each one once.
        for warning in result.warnings:
            if warning not in warned:
                warned.add(warning)
                print(f"  !! {warning}", flush=True)

        fetched += manifest.bytes_fetched
        written += int(did_write)
        status = "written" if did_write else "cached"
        print(
            f"  [{index}/{len(releases)}] {release}  "
            f"{manifest.row_count:>6} variants  "
            f"{_human_bytes(manifest.bytes_fetched):>9}  {status}",
            flush=True,
        )

    manifests = snapshot.load_manifests(cfg.snapshot_dir, panel_slug)
    if manifests:
        full = manifests[-1].source_full_bytes or 0
        naive = full * len(manifests)
        print(
            f"\ningested {len(manifests)} snapshots ({written} new), "
            f"{_human_bytes(fetched)} over the wire"
        )
        if naive:
            print(
                f"whole-file downloads would have moved {_human_bytes(naive)} "
                f"({naive / max(fetched, 1):.0f}x more)"
            )
    return 0


def cmd_spans(args) -> int:
    cfg = config_module.load()
    panel_slug = genes.load_panel(_resolve_panel(args.panel))[0]
    manifests = snapshot.load_manifests(cfg.snapshot_dir, panel_slug)
    if not manifests:
        print(f"no snapshots for panel {panel_slug!r}; run ingest first", file=sys.stderr)
        return 1

    with connect(cfg.warehouse) as connection:
        count = spans.build(connection, cfg.snapshot_dir, manifests, panel_slug)
        alleles = connection.execute(
            "SELECT COUNT(DISTINCT allele_id) FROM assertion_span"
        ).fetchone()[0]
        changed = connection.execute(
            "SELECT COUNT(*) FROM (SELECT allele_id FROM assertion_span "
            "GROUP BY allele_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]

    print(f"{count} spans across {alleles} alleles")
    print(f"{changed} alleles changed state at least once "
          f"({changed / max(alleles, 1):.1%})")
    return 0


def cmd_spike(args) -> int:
    """The Phase 0 gate: how much of what was VUS has since moved?"""
    cfg = config_module.load()
    panel_slug = genes.load_panel(_resolve_panel(args.panel))[0]
    manifests = snapshot.load_manifests(cfg.snapshot_dir, panel_slug)
    if not manifests:
        print("no snapshots; run ingest first", file=sys.stderr)
        return 1

    baseline = args.baseline or manifests[0].release_date
    current = args.current or manifests[-1].release_date

    with connect(cfg.warehouse, read_only=True) as connection:
        events = policy.detect(connection, threshold=args.policy_threshold)
        summary = movement.panel_movement(
            connection, baseline, current, suspect=policy.suspect_pairs(events)
        )
        detail = movement.movement_detail(connection, baseline, current, limit=args.limit)

    print()
    print(f"  MENDELEA PHASE 0  --  panel '{panel_slug}'")
    print(f"  {summary.baseline_date}  ->  {summary.current_date}")
    print("  " + "-" * 62)
    print(f"  variants classified at baseline      {summary.total_tracked:>8,}")
    print(f"  of which VUS                         {summary.vus_at_baseline:>8,}")
    print("  " + "-" * 62)
    print(f"  VUS that moved                       {summary.vus_moved:>8,}   "
          f"{summary.movement_rate:>6.1%}")
    print(f"    -> pathogenic / likely pathogenic  {summary.moved_to_pathogenic:>8,}")
    print(f"    -> benign / likely benign          {summary.moved_to_benign:>8,}")
    print(f"    -> conflicting                     {summary.moved_to_conflicting:>8,}")
    print(f"    -> retracted from ClinVar          {summary.retracted:>8,}")
    print("  " + "-" * 62)

    if events:
        print("  POLICY EVENTS DETECTED -- database relabelling, not evidence:")
        for event in events:
            print(f"    ! {event.describe()}")
        print(f"  of the moves above, policy-suspect      {summary.policy_suspect:>8,}")
        print(f"  movement rate, policy-adjusted       "
              f"{summary.movement_rate_adjusted:>13.1%}  "
              f"(raw {summary.movement_rate:.1%})")
        print("  " + "-" * 62)

    print(f"  ACTIONABLE movement rate             {summary.actionable_rate:>13.1%}")
    print("  (actionable excludes CONFLICTING, so policy events cannot inflate it)")
    print("  " + "-" * 62)
    print(f"  Gate: continue if actionable >= 2.0%  "
          f"[{'PASS' if summary.actionable_rate >= 0.02 else 'FAIL'}]")
    print()

    if detail:
        print(f"  Sample of moved variants (top {len(detail)} by current review status):")
        print(f"  {'gene':<8} {'variation':>10}  {'was':<12} -> {'now':<20} {'stars':>5}")
        for row in detail:
            _, gene, variation_id, from_bucket, _, to_bucket, to_stars, _ = row
            print(f"  {gene or '?':<8} {variation_id:>10}  "
                  f"{from_bucket:<12} -> {to_bucket:<20} {to_stars:>5}")
        print()
    return 0


def cmd_timeline(args) -> int:
    """What the evidence said about a gene's variants on a given date."""
    cfg = config_module.load()
    with connect(cfg.warehouse, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT gene, variation_id, contig, pos, ref, alt, bucket, stars
            FROM assertion_span
            WHERE gene = ?
              AND valid_from <= CAST(? AS DATE)
              AND valid_to   >  CAST(? AS DATE)
              AND bucket <> 'ABSENT'
            ORDER BY pos
            LIMIT ?
            """,
            [args.gene.upper(), args.on, args.on, args.limit],
        ).fetchall()

    print(f"\n  {args.gene.upper()} as ClinVar saw it on {args.on}  "
          f"({len(rows)} variants shown)\n")
    for gene, variation_id, contig, pos, ref, alt, bucket, star in rows:
        print(f"  {contig}:{pos:<10} {ref:>4}>{alt:<4}  {variation_id:>10}  "
              f"{bucket:<18} {'*' * max(star, 0)}")
    print()
    return 0


def _reference_for_panel(cfg, panel_slug: str):
    """Load reference sequence for a panel's gene regions, for left-alignment.

    Degrades to None with a warning rather than failing the load: trim-only
    normalisation still matches everything already left-aligned, which is most
    of a real file. Silently degrading would be the unacceptable option.
    """
    from .evidence.reference import ReferenceCache

    try:
        _, symbols = genes.load_panel(_resolve_panel(panel_slug))
        resolver = genes.GeneResolver(
            cache_path=cfg.reference_dir / "gene_regions.json",
            bundled=PANEL_DIR / "gene_regions.json",
        )
        reference = ReferenceCache(cfg.reference_dir / "sequence")
        for region in resolver.resolve_all(symbols):
            contig, start, end = region.padded()
            reference.load_region(contig, start, end)
        return reference
    except Exception as exc:  # noqa: BLE001
        print(f"  ! reference unavailable ({exc}); indels will not be left-aligned",
              file=sys.stderr)
        return None


def cmd_tenant_create(args) -> int:
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        try:
            tenancy.create_tenant(connection, args.tenant, args.name)
        except tenancy.AuthError as exc:
            if not args.token_only:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        token = tenancy.issue_token(connection, args.tenant, label=args.label)

    print(f"tenant  {token.tenant_id}")
    print(f"token   {token.plaintext}")
    print("\n  This is shown once and is not recoverable. Only its hash is stored.")
    return 0


def cmd_tenant_list(args) -> int:
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        tenants = tenancy.list_tenants(connection)
        detail = {t["tenant_id"]: tenancy.list_tokens(connection, t["tenant_id"])
                  for t in tenants}

    if not tenants:
        print("no tenants registered")
        return 0
    for tenant in tenants:
        print(f"  {tenant['tenant_id']:<16} {tenant['name']:<24} "
              f"{tenant['active_tokens']} active, {tenant['revoked_tokens']} revoked")
        for token in detail[tenant["tenant_id"]]:
            state = "revoked" if token["revoked_at"] else "active"
            print(f"      {token['token_id']}  {state:<8} {token['label']}")
    return 0


def cmd_tenant_revoke(args) -> int:
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        done = tenancy.revoke(connection, args.token_id)
    print(f"token {args.token_id}: " + ("revoked" if done else "unknown or already revoked"))
    return 0 if done else 1


def cmd_case_demo(args) -> int:
    """Write a synthetic laboratory export drawn from the evidence plane.

    Lets the whole Phase 3 flow be demonstrated before any real customer file
    exists. Sampled from variants that were genuinely uncertain at an early
    release, with sign-out dates spread realistically after that.
    """
    import csv
    import random

    cfg = config_module.load()
    random.seed(args.seed)

    with connect(cfg.warehouse, read_only=True) as connection:
        # Only dates this panel was actually observed on: sampling another
        # panel's dates would produce sign-outs we can only answer with
        # carried-forward state.
        panel = spans.loaded_panel(connection)
        releases = spans.release_dates(connection, panel)
        if not releases:
            print("no releases for the loaded panel", file=sys.stderr)
            return 1
        earliest = releases[0]
        rows = connection.execute(
            """
            SELECT gene, contig, pos, ref, alt, bucket
            FROM assertion_span
            WHERE valid_from <= CAST($on AS DATE) AND valid_to > CAST($on AS DATE)
              AND bucket IN ('UNCERTAIN','LIKELY_PATHOGENIC','PATHOGENIC')
            ORDER BY random() LIMIT $n
            """,
            {"on": earliest, "n": args.count},
        ).fetchall()

    # Sign-outs land on the earlier half of our coverage, so most rows have a
    # baseline to compare against. A deliberate few predate coverage entirely,
    # so the reconciliation section has something real to report.
    usable = releases[: max(1, len(releases) // 2)]
    before_coverage = date.fromisoformat(earliest).replace(
        year=date.fromisoformat(earliest).year - 3
    ).isoformat()

    label = {"UNCERTAIN": "VUS", "LIKELY_PATHOGENIC": "Likely pathogenic",
             "PATHOGENIC": "Pathogenic"}
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_ref", "gene", "contig", "pos", "ref", "alt",
                         "reported_classification", "reported_on"])
        for index, (gene, contig, pos, ref, alt, bucket) in enumerate(rows, start=1):
            signed = before_coverage if random.random() < 0.05 else random.choice(usable)
            writer.writerow([f"CASE-{index:05d}", gene, contig, pos, ref, alt,
                             label[bucket], signed])

    print(f"wrote {len(rows)} synthetic reported variants to {out}")
    print(f"sign-out dates drawn from {usable[0]} .. {usable[-1]}, "
          f"with ~5% deliberately predating coverage")
    return 0


def cmd_case_load(args) -> int:
    cfg = config_module.load()
    reference = None if args.no_reference else _reference_for_panel(cfg, args.panel)

    variants, rejected = model.load_csv(
        Path(args.file), tenant_id=args.tenant, assembly=cfg.assembly,
        reference=reference,
    )

    with connect(cfg.warehouse) as connection:
        connection.execute(model.SCHEMA_DDL)
        connection.execute("DELETE FROM case_variant WHERE tenant_id = ?", [args.tenant])
        loaded = model.persist(connection, variants)

    print(f"loaded {loaded} variants for tenant {args.tenant!r}")
    if rejected:
        print(f"rejected {len(rejected)} rows:")
        for line in rejected[:10]:
            print(f"    {line}")
    return 0


def cmd_case_report(args) -> int:
    cfg = config_module.load()
    with connect(cfg.warehouse, read_only=True) as connection:
        events = policy.detect(connection)
        rep = case_report.build(connection, args.tenant, policy.suspect_pairs(events))

    r = rep.reconciliation
    print("\n  MENDELEA REANALYSIS REPORT")
    print(f"  tenant {rep.tenant_id}   evidence: {rep.evidence_panel}, "
          f"{rep.evidence_from} .. {rep.evidence_to}")
    print("  " + "=" * 64)
    print("  RECONCILIATION   (every input row is accounted for)")
    print(f"    variants loaded                  {r.loaded:>7,}")
    print(f"    not found in evidence            {r.unmatched:>7,}")
    print(f"    signed out before coverage       {r.before_coverage:>7,}")
    print(f"    examined                         {r.examined:>7,}   {r.match_rate:.1%}")
    if not r.is_sound():
        print("    ** match rate below 90%: movement figures below are NOT")
        print("       representative of this laboratory's back catalogue **")
    print("  " + "-" * 64)
    print(f"    unchanged since sign-out         {rep.unchanged:>7,}")
    print(f"    moved                            {rep.moved:>7,}")
    print(f"      -> pathogenic / likely         {rep.moved_to_pathogenic:>7,}")
    print(f"      -> benign / likely             {rep.moved_to_benign:>7,}")
    print(f"      -> conflicting                 {rep.moved_to_conflicting:>7,}"
          f"   ({rep.policy_suspect} policy-suspect)")
    print(f"      -> retracted from ClinVar      {rep.retracted:>7,}")
    print("  " + "=" * 64)
    print(f"    ACTIONABLE                       {rep.actionable:>7,}   "
          f"{rep.actionable_rate:.1%} of examined")
    print("  " + "=" * 64)

    for finding in rep.findings[:args.limit]:
        flag = "!" if finding["actionable"] else ("~" if finding["policy_suspect"] else " ")
        print(f"  {flag} {finding['case_ref']}  {finding['gene']:<8} "
              f"{finding['variant']:<24} {finding['evidence_at_signout']:<12} -> "
              f"{finding['evidence_now']:<18} {'*' * (finding['review_status_now'] or 0)}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({
                "tenant": rep.tenant_id,
                "evidence_panel": rep.evidence_panel,
                "evidence_from": rep.evidence_from,
                "evidence_to": rep.evidence_to,
                "reconciliation": rep.reconciliation.__dict__,
                "findings": rep.findings,
            }, indent=2),
            encoding="utf-8",
        )
        print(f"\n  wrote {args.out}")
    return 0


def cmd_case_review(args) -> int:
    """Record a curator's decision about one moved variant.

    Stamped with the evidence snapshot in view at the time, so the ledger says
    not just what was decided but what it was decided against.
    """
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        panel = spans.loaded_panel(connection)
        snapshot_id = connection.execute(
            "SELECT snapshot_id FROM snapshot_manifest WHERE panel = ? "
            "ORDER BY release_date DESC LIMIT 1",
            [panel],
        ).fetchone()
        if not snapshot_id:
            print("no evidence snapshot to anchor the decision to", file=sys.stderr)
            return 1

        entry = ledger.append(
            connection,
            tenant_id=args.tenant,
            allele_id=args.allele_id,
            case_ref=args.case_ref,
            verdict=args.verdict,
            rationale=args.rationale,
            reviewer=args.reviewer,
            evidence_snapshot_id=snapshot_id[0],
        )

    print(f"recorded #{entry.seq} {entry.verdict} for {entry.case_ref} "
          f"({entry.allele_id[:24]}…)")
    print(f"  against {entry.evidence_snapshot_id}")
    print(f"  hash {entry.entry_hash[:16]}… chained to {entry.prev_hash[:16]}…")
    return 0


def cmd_case_log(args) -> int:
    """Show a tenant's decision ledger, newest last, with its integrity state."""
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        ledger.init(connection)
        rows = connection.execute(
            "SELECT seq, created_at, case_ref, verdict, reviewer, rationale "
            "FROM decision_ledger WHERE tenant_id = ? ORDER BY seq",
            [args.tenant],
        ).fetchall()
        intact, bad = ledger.verify(connection, args.tenant)

    if not rows:
        print(f"no decisions recorded for {args.tenant!r}")
        return 0

    for seq, created_at, case_ref, verdict, reviewer, rationale in rows:
        print(f"  #{seq:<4} {created_at}  {case_ref:<12} {verdict:<10} "
              f"{reviewer:<20} {rationale}")
    print("\n  chain " + ("intact" if intact else f"BROKEN at entry {bad}"))
    return 0 if intact else 1


def cmd_case_verify(args) -> int:
    cfg = config_module.load()
    with connect(cfg.warehouse) as connection:
        intact, bad = ledger.verify(connection, args.tenant)
    print(f"decision ledger for {args.tenant!r}: "
          + ("intact" if intact else f"BROKEN at entry {bad}"))
    return 0 if intact else 1


def cmd_serve(args) -> int:
    """Run the time machine."""
    cfg = config_module.load()
    if not cfg.warehouse.exists():
        print("no warehouse; run ingest then spans first", file=sys.stderr)
        return 1

    from .web.server import serve

    serve(cfg.warehouse, host=args.host, port=args.port)
    return 0


def cmd_provenance(args) -> int:
    """Audit every snapshot: does its content still hash to what we recorded,
    and do we know which code produced it?

    Two different questions. The checksum proves the data has not changed
    since it was written; the git sha says whether the pipeline that wrote it
    can be reconstructed. A snapshot can pass the first and fail the second.
    """
    cfg = config_module.load()
    manifests = snapshot.load_manifests(cfg.snapshot_dir, args.panel)
    if not manifests:
        print("no snapshots found", file=sys.stderr)
        return 1

    from collections import Counter

    reproducible = Counter()
    tampered, missing, warned = [], [], []

    for manifest in manifests:
        path = snapshot.snapshot_path(
            cfg.snapshot_dir, date.fromisoformat(manifest.release_date), manifest.panel
        )
        if not path.exists():
            missing.append(manifest.snapshot_id)
        elif snapshot._sha256(path) != manifest.parquet_sha256:
            tampered.append(manifest.snapshot_id)

        sha = manifest.pipeline_git_sha
        reproducible["unknown" if sha == "unknown"
                     else "dirty" if sha.endswith("-dirty")
                     else "clean"] += 1
        if manifest.gene_warnings:
            warned.append(manifest.snapshot_id)

    print(f"\n  {len(manifests)} snapshots audited\n")
    print("  CONTENT INTEGRITY")
    print(f"    checksum matches            {len(manifests) - len(tampered) - len(missing):>5}")
    print(f"    checksum MISMATCH           {len(tampered):>5}" + ("  <-- altered since written" if tampered else ""))
    print(f"    file missing                {len(missing):>5}")
    print("\n  CODE PROVENANCE")
    print(f"    clean tree (reproducible)   {reproducible['clean']:>5}")
    print(f"    dirty tree                  {reproducible['dirty']:>5}")
    print(f"    no git checkout             {reproducible['unknown']:>5}")
    if warned:
        print(f"\n  {len(warned)} snapshots carry region warnings")

    if reproducible["unknown"] or reproducible["dirty"]:
        print("\n  Snapshots without a clean git sha are still content-verified,")
        print("  but the pipeline that produced them cannot be reconstructed.")
        print("  Re-ingest with --force to restamp them, or treat as provisional.")

    for label, items in (("MISMATCH", tampered), ("MISSING", missing)):
        for item in items[:10]:
            print(f"    {label}: {item}")

    return 1 if (tampered or missing) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mendelea", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="fetch archived releases into immutable snapshots")
    p.add_argument("--panel", default="spike")
    p.add_argument("--from", dest="start", type=int, default=2019)
    p.add_argument("--to", dest="end", type=int, default=date.today().year)
    p.add_argument("--per-year", type=int, default=2)
    p.add_argument("--force", action="store_true",
                   help="steal the ingest lock from a run presumed dead")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("provenance", help="audit snapshot provenance")
    p.add_argument("--panel", default=None)
    p.set_defaults(func=cmd_provenance)

    p = sub.add_parser("serve", help="run the evidence time machine")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("tenant-create", help="register a tenant and mint a token")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--label", default="", help="what this token is for")
    p.add_argument("--token-only", action="store_true",
                   help="tenant already exists; just mint another token")
    p.set_defaults(func=cmd_tenant_create)

    p = sub.add_parser("tenant-list", help="list tenants and their tokens")
    p.set_defaults(func=cmd_tenant_list)

    p = sub.add_parser("tenant-revoke", help="revoke a token by its id")
    p.add_argument("--token-id", required=True)
    p.set_defaults(func=cmd_tenant_revoke)

    p = sub.add_parser("case-demo", help="write a synthetic laboratory export")
    p.add_argument("--out", default="demo-cases.csv")
    p.add_argument("--count", type=int, default=500)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_case_demo)

    p = sub.add_parser("case-load", help="load a laboratory's reported variants")
    p.add_argument("--tenant", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--panel", default="hereditary-cancer")
    p.add_argument("--no-reference", action="store_true",
                   help="skip left-alignment (faster, but indels may not match)")
    p.set_defaults(func=cmd_case_load)

    p = sub.add_parser("case-report", help="what has moved under a tenant's variants")
    p.add_argument("--tenant", required=True)
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--out", default=None, help="also write findings as JSON")
    p.set_defaults(func=cmd_case_report)

    p = sub.add_parser("case-review", help="record a decision about a moved variant")
    p.add_argument("--tenant", required=True)
    p.add_argument("--case-ref", required=True)
    p.add_argument("--allele-id", required=True)
    p.add_argument("--verdict", required=True, choices=sorted(ledger.VERDICTS))
    p.add_argument("--rationale", default="")
    p.add_argument("--reviewer", required=True)
    p.set_defaults(func=cmd_case_review)

    p = sub.add_parser("case-log", help="show a tenant's decision ledger")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_case_log)

    p = sub.add_parser("case-verify", help="check a tenant's decision ledger")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_case_verify)

    p = sub.add_parser("spans", help="rebuild the bitemporal assertion timeline")
    p.add_argument("--panel", default="spike")
    p.set_defaults(func=cmd_spans)

    p = sub.add_parser("spike", help="Phase 0 gate: measure VUS movement")
    p.add_argument("--panel", default="spike")
    p.add_argument("--baseline", default=None)
    p.add_argument("--current", default=None)
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--policy-threshold", type=float, default=policy.DEFAULT_THRESHOLD,
                   help="corpus share above which a single-step transition is "
                        "treated as a database policy change, not evidence")
    p.set_defaults(func=cmd_spike)

    p = sub.add_parser("timeline", help="evidence state for a gene on a date")
    p.add_argument("--gene", required=True)
    p.add_argument("--on", required=True)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_timeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
