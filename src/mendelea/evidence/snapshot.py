"""Immutable evidence snapshots and their manifest.

The rule this module exists to enforce: a snapshot is written once and never
touched again. The previous generation of this project rebuilt "current
state" every Thursday, which quietly destroyed the past each week and made
reanalysis impossible by construction. Here, every release becomes its own
content-addressed file, and the timeline is derived from the accumulation.

Storage is Parquet on disk, queried in place by DuckDB. The same layout moves
to object storage unchanged, which is what keeps the evidence plane shareable
across every tenant and every deployment mode.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from . import clinvar, tabix
from .bgzf import decompress, iter_complete_lines
from .genes import GeneRegion, verify_regions
from .normalize import allele_id
from .remote import RemoteFile, fetch_cached

# The snapshot schema, as DuckDB column declarations. DuckDB already read
# every snapshot in place; it now writes them too. pyarrow used to do the
# writing and was, by an order of magnitude, the largest dependency in the
# project. Its native library ships unsigned, and Windows Smart App Control
# blocked it outright on the machine this project is developed on -- the "one
# more thing that can fail on someone else's laptop" the README warns about,
# except it was ours.
COLUMNS = (
    ("allele_id", "VARCHAR"),
    ("variation_id", "VARCHAR"),
    ("contig", "VARCHAR"),
    ("pos", "BIGINT"),
    ("ref", "VARCHAR"),
    ("alt", "VARCHAR"),
    ("gene", "VARCHAR"),
    ("bucket", "VARCHAR"),
    ("stars", "TINYINT"),
    ("clnsig_raw", "VARCHAR"),
    ("clnrevstat_raw", "VARCHAR"),
    ("condition", "VARCHAR"),
)

_COLUMN_LIST = ", ".join(name for name, _ in COLUMNS)
_COLUMN_TYPES = ", ".join(f"'{name}': '{kind}'" for name, kind in COLUMNS)


def write_snapshot(path: Path, rows: list[dict]) -> None:
    """Write `rows` (dicts keyed by COLUMNS) as a Parquet snapshot, atomically.

    Rows travel through a JSON-lines spill file rather than parameter
    binding: DuckDB's executemany costs about 10 ms per row, which for a
    30,000-variant gene is five minutes; its JSON reader loads the same rows
    in a third of a second. JSON also keeps NULL and empty string apart,
    which CSV would not. Types are declared rather than inferred, so an
    all-NULL column in a small snapshot cannot come out as something else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    spill = path.with_suffix(".rows.jsonl")
    partial = path.with_suffix(".parquet.partial")
    try:
        with spill.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({name: row.get(name) for name, _ in COLUMNS}))
                handle.write("\n")

        connection = duckdb.connect()
        try:
            connection.execute(
                f"COPY (SELECT {_COLUMN_LIST} FROM read_json($spill, "
                f"format = 'newline_delimited', columns = {{{_COLUMN_TYPES}}})) "
                "TO $target (FORMAT PARQUET, COMPRESSION ZSTD)",
                {"spill": str(spill), "target": str(partial)},
            )
        finally:
            connection.close()

        partial.replace(path)  # atomic: a killed run leaves no half-snapshot behind
    finally:
        spill.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


@dataclass
class SnapshotManifest:
    """Provenance for one snapshot. Every downstream fact traces back to a row here."""

    snapshot_id: str
    source: str
    release_date: str
    panel: str
    source_url: str
    retrieved_at: str
    parquet_sha256: str
    row_count: int
    bytes_fetched: int
    source_full_bytes: int
    genes_requested: int
    pipeline_git_sha: str
    # Defaulted so manifests written before the check existed still load.
    gene_warnings: tuple[str, ...] = ()


@dataclass
class IngestResult:
    manifest: SnapshotManifest
    written: bool
    warnings: list[str]


def _git_sha() -> str:
    """Record which code produced a snapshot, so results stay reproducible.

    Three possible answers, and the distinctions matter:

        "a1b2c3d"        clean tree -- the snapshot can be reproduced exactly
        "a1b2c3d-dirty"  uncommitted changes were present
        "unknown"        not a git checkout at all

    A dirty tree is called out explicitly because a snapshot built from
    uncommitted code is no more reproducible than one built outside version
    control. Recording the bare sha in that case would be worse than recording
    nothing: it asserts a provenance that cannot be recovered.
    """
    here = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:  # noqa: BLE001 - no git, or not a checkout
        return "unknown"

    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=here, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:  # noqa: BLE001 - sha is still better than nothing
        return sha

    return f"{sha}-dirty" if dirty else sha


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_path(root: Path, release_date: date, panel_slug: str) -> Path:
    return root / "clinvar" / release_date.isoformat() / f"{panel_slug}.parquet"


def manifest_path(root: Path, release_date: date, panel_slug: str) -> Path:
    return snapshot_path(root, release_date, panel_slug).with_suffix(".manifest.json")


def ingest_release(
    release: clinvar.Release,
    regions: list[GeneRegion],
    panel_slug: str,
    snapshot_root: Path,
    cache_dir: Path,
    assembly: str = "GRCh38",
    force: bool = False,
) -> IngestResult:
    """Fetch one release restricted to `regions` and write an immutable snapshot.

    Returns an IngestResult whose `written` is False when the snapshot already
    existed, which is the normal case on re-runs -- snapshots are never
    rewritten, only skipped.
    """
    target = snapshot_path(snapshot_root, release.release_date, panel_slug)
    meta_target = manifest_path(snapshot_root, release.release_date, panel_slug)

    if target.exists() and meta_target.exists() and not force:
        stored = json.loads(meta_target.read_text(encoding="utf-8"))
        stored["gene_warnings"] = tuple(stored.get("gene_warnings", ()))
        manifest = SnapshotManifest(**stored)
        return IngestResult(manifest, False, list(manifest.gene_warnings))

    index_raw = decompress(fetch_cached(release.tbi_url, cache_dir))
    index = tabix.parse(index_raw)

    remote = RemoteFile(release.vcf_url)
    remote.check_range_support()
    remote.size()

    rows: dict[str, dict] = {}
    # Gene symbols ClinVar itself reports inside each resolved region. Used to
    # catch coordinates from the wrong assembly, which would otherwise return
    # a plausible-looking but wrong stretch of the genome.
    observed: dict[str, set[str]] = {}

    for region in regions:
        contig, start, end = region.padded()
        # Tabix is 0-based half-open internally; our coordinates are 1-based.
        ranges = index.byte_ranges(contig, start - 1, end)
        if not ranges:
            continue

        payload = remote.read_ranges(ranges)
        for line in iter_complete_lines(decompress(payload)):
            record = clinvar.parse_vcf_line(line)
            if record is None:
                continue
            if record.contig.removeprefix("chr") != contig.removeprefix("chr"):
                continue
            if not (start <= record.pos <= end):
                continue

            try:
                key = allele_id(assembly, record.contig, record.pos, record.ref, record.alt)
            except ValueError:
                continue  # symbolic or malformed allele; not representable

            if record.gene:
                observed.setdefault(region.symbol, set()).add(record.gene)

            # Overlapping padded regions can deliver the same record twice.
            rows[key] = {
                "allele_id": key,
                "variation_id": record.variation_id,
                "contig": record.contig,
                "pos": record.pos,
                "ref": record.ref,
                "alt": record.alt,
                "gene": record.gene or region.symbol,
                "bucket": record.bucket,
                "stars": record.stars,
                "clnsig_raw": record.clnsig_raw,
                "clnrevstat_raw": record.clnrevstat_raw,
                "condition": record.condition,
            }

    warnings = verify_regions(regions, observed)

    ordered = sorted(rows.values(), key=lambda r: (r["contig"], r["pos"], r["allele_id"]))
    write_snapshot(target, ordered)

    manifest = SnapshotManifest(
        snapshot_id=f"clinvar-{release.stamp}-{panel_slug}",
        source="clinvar",
        release_date=release.release_date.isoformat(),
        panel=panel_slug,
        source_url=release.vcf_url,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        parquet_sha256=_sha256(target),
        row_count=len(ordered),
        bytes_fetched=remote.stats.bytes_fetched,
        source_full_bytes=remote.stats.full_size,
        genes_requested=len(regions),
        pipeline_git_sha=_git_sha(),
        gene_warnings=tuple(warnings),
    )
    meta_target.write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")

    return IngestResult(manifest, True, warnings)


def load_manifests(snapshot_root: Path, panel_slug: str | None = None) -> list[SnapshotManifest]:
    """Read every manifest on disk, newest last."""
    manifests = []
    for path in sorted((snapshot_root / "clinvar").glob("*/*.manifest.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if panel_slug and payload.get("panel") != panel_slug:
            continue
        payload["gene_warnings"] = tuple(payload.get("gene_warnings", ()))
        manifests.append(SnapshotManifest(**payload))
    return sorted(manifests, key=lambda m: m.release_date)
