"""Command line entry point.

    mendelea ingest --panel spike --from 2019 --to 2026 --per-year 2
    mendelea spans  --panel spike
    mendelea spike  --panel spike
    mendelea timeline --gene BRCA1 --on 2019-06-03
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import config as config_module
from . import locks
from .db import connect
from .evidence import clinvar, genes, snapshot, spans
from .reports import movement, policy

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = REPO_ROOT / "panels"


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
