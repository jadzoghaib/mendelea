"""The bitemporal assertion timeline.

`assertion_span` is the table the whole product reduces to. One row per
(allele, unbroken run of identical classification), with the release dates it
was valid between. Once it exists:

    "what did ClinVar say about X on date D"   -> a range predicate
    "which reported variants have since moved" -> one join
    "prove it"                                 -> join to snapshot_manifest

Absence is a state. If an allele present in one release is missing from the
next, that is a retraction, and a laboratory that reported it wants to know.
Densifying over the full release grid is what makes retractions visible
instead of silently ending a span.

Caveat worth keeping in view: absence is only meaningful because every
snapshot for a panel is ingested over identical gene regions. Change the
panel and the ABSENT transitions become artefacts, which is why spans are
built per panel and never across panels.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path

from ..db import as_posix
from .snapshot import SnapshotManifest, snapshot_path

ABSENT = "ABSENT"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS snapshot_manifest (
    snapshot_id        VARCHAR PRIMARY KEY,
    source             VARCHAR,
    release_date       DATE,
    panel              VARCHAR,
    source_url         VARCHAR,
    retrieved_at       VARCHAR,
    parquet_sha256     VARCHAR,
    row_count          BIGINT,
    bytes_fetched      BIGINT,
    source_full_bytes  BIGINT,
    genes_requested    INTEGER,
    pipeline_git_sha   VARCHAR
);
"""


def record_manifests(connection, manifests: list[SnapshotManifest]) -> None:
    """Upsert manifest rows. Snapshots are immutable, so this is idempotent."""
    connection.execute(SCHEMA_DDL)
    columns = (
        "snapshot_id", "source", "release_date", "panel", "source_url",
        "retrieved_at", "parquet_sha256", "row_count", "bytes_fetched",
        "source_full_bytes", "genes_requested", "pipeline_git_sha",
    )
    for manifest in manifests:
        # Only the columns the table declares: the manifest also carries
        # gene_warnings, which belongs in the sidecar file, not this table.
        row = {k: v for k, v in asdict(manifest).items() if k in columns}
        connection.execute(
            """
            INSERT INTO snapshot_manifest VALUES (
                $snapshot_id, $source, CAST($release_date AS DATE), $panel,
                $source_url, $retrieved_at, $parquet_sha256, $row_count,
                $bytes_fetched, $source_full_bytes, $genes_requested,
                $pipeline_git_sha
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            row,
        )


def build(connection, snapshot_root: Path, manifests: list[SnapshotManifest],
          panel_slug: str) -> int:
    """Rebuild `assertion_span` for one panel from its snapshots.

    Returns the number of spans produced.
    """
    if not manifests:
        raise ValueError("no snapshots to build spans from")

    record_manifests(connection, manifests)

    # 1. Stack every snapshot into one long table, stamped with its release
    #    date. This is the only place release date enters the data -- the
    #    snapshot files themselves stay minimal and self-similar.
    connection.execute("DROP TABLE IF EXISTS assertion_raw")
    connection.execute(
        """
        CREATE TABLE assertion_raw (
            allele_id VARCHAR, variation_id VARCHAR, gene VARCHAR,
            contig VARCHAR, pos BIGINT, ref VARCHAR, alt VARCHAR,
            bucket VARCHAR, stars TINYINT,
            clnsig_raw VARCHAR, clnrevstat_raw VARCHAR, condition VARCHAR,
            release_date DATE, snapshot_id VARCHAR
        )
        """
    )

    for manifest in manifests:
        path = snapshot_path(
            snapshot_root, date.fromisoformat(manifest.release_date), panel_slug
        )
        if not path.exists():
            continue
        connection.execute(
            """
            INSERT INTO assertion_raw
            SELECT allele_id, variation_id, gene, contig, pos, ref, alt,
                   bucket, stars, clnsig_raw, clnrevstat_raw, condition,
                   CAST(? AS DATE), ?
            FROM read_parquet(?)
            """,
            [manifest.release_date, manifest.snapshot_id, as_posix(path)],
        )

    # 2. Stable per-allele attributes, taken from the most recent release that
    #    carried the allele.
    connection.execute("DROP TABLE IF EXISTS allele_dim")
    connection.execute(
        """
        CREATE TABLE allele_dim AS
        SELECT allele_id,
               last(variation_id ORDER BY release_date) AS variation_id,
               last(gene         ORDER BY release_date) AS gene,
               last(contig       ORDER BY release_date) AS contig,
               last(pos          ORDER BY release_date) AS pos,
               last(ref          ORDER BY release_date) AS ref,
               last(alt          ORDER BY release_date) AS alt,
               last(condition    ORDER BY release_date) AS condition
        FROM assertion_raw
        GROUP BY allele_id
        """
    )

    # 3. Densify over the full release grid so absence becomes an explicit
    #    state rather than a gap.
    connection.execute("DROP TABLE IF EXISTS assertion_dense")
    connection.execute(
        f"""
        CREATE TABLE assertion_dense AS
        SELECT a.allele_id,
               r.release_date,
               COALESCE(s.bucket, '{ABSENT}')   AS bucket,
               COALESCE(s.stars, CAST(-1 AS TINYINT)) AS stars,
               s.clnsig_raw,
               s.clnrevstat_raw,
               s.snapshot_id
        FROM (SELECT DISTINCT allele_id FROM assertion_raw) a
        CROSS JOIN (SELECT DISTINCT release_date FROM assertion_raw) r
        LEFT JOIN assertion_raw s
               ON s.allele_id = a.allele_id
              AND s.release_date = r.release_date
        """
    )

    # 4. Collapse consecutive identical states into spans (a slowly-changing
    #    dimension, type 2). A span breaks when bucket or star rating changes.
    connection.execute("DROP TABLE IF EXISTS assertion_span")
    connection.execute(
        """
        CREATE TABLE assertion_span AS
        WITH ordered AS (
            SELECT *,
                   LAG(bucket) OVER w AS prev_bucket,
                   LAG(stars)  OVER w AS prev_stars
            FROM assertion_dense
            WINDOW w AS (PARTITION BY allele_id ORDER BY release_date)
        ),
        marked AS (
            SELECT *,
                   CASE WHEN prev_bucket IS NULL
                          OR prev_bucket IS DISTINCT FROM bucket
                          OR prev_stars  IS DISTINCT FROM stars
                        THEN 1 ELSE 0 END AS is_change
            FROM ordered
        ),
        numbered AS (
            SELECT *,
                   SUM(is_change) OVER (
                       PARTITION BY allele_id ORDER BY release_date
                       ROWS UNBOUNDED PRECEDING
                   ) AS span_no
            FROM marked
        ),
        collapsed AS (
            SELECT allele_id,
                   span_no,
                   any_value(bucket)         AS bucket,
                   any_value(stars)          AS stars,
                   any_value(clnsig_raw)     AS clnsig_raw,
                   any_value(clnrevstat_raw) AS clnrevstat_raw,
                   MIN(release_date)         AS valid_from,
                   MAX(release_date)         AS last_seen,
                   MIN(snapshot_id)          AS first_snapshot_id
            FROM numbered
            GROUP BY allele_id, span_no
        )
        SELECT ? AS panel,
               c.allele_id,
               d.variation_id,
               d.gene,
               d.contig,
               d.pos,
               d.ref,
               d.alt,
               d.condition,
               c.bucket,
               c.stars,
               c.clnsig_raw,
               c.clnrevstat_raw,
               c.valid_from,
               -- Open-ended spans use a sentinel far future date so that
               -- BETWEEN predicates need no special casing.
               COALESCE(
                   LEAD(c.valid_from) OVER (PARTITION BY c.allele_id ORDER BY c.valid_from),
                   DATE '9999-12-31'
               ) AS valid_to,
               c.last_seen,
               c.first_snapshot_id,
               LEAD(c.valid_from) OVER (
                   PARTITION BY c.allele_id ORDER BY c.valid_from
               ) IS NULL AS is_current
        FROM collapsed c
        JOIN allele_dim d USING (allele_id)
        ORDER BY c.allele_id, c.valid_from
        """,
        [panel_slug],
    )

    return connection.execute("SELECT COUNT(*) FROM assertion_span").fetchone()[0]


# What a read-only public deployment serves, and nothing else. `assertion_raw`
# and `allele_dim` are build intermediates; the case, decision and tenant
# tables are the private planes. Leaving the private tables out is not
# tidiness -- a build that does not contain them cannot leak them, whatever
# the auth layer does, and that is a far easier claim to make to a hospital's
# security review than "we checked the queries".
PUBLIC_TABLES = ("assertion_span", "snapshot_manifest", "assertion_dense")


def export_public(source: Path, target: Path) -> dict[str, int]:
    """Copy just the public evidence tables into a fresh, smaller warehouse.

    Returns the row count written per table. The result is what ships in a
    container image: on the 31-gene panel it is 58 MB against the 308 MB
    working warehouse, because the build intermediates are four times the
    size of the timeline they produce.
    """
    import duckdb

    if not source.exists():
        raise FileNotFoundError(f"no warehouse at {source}")
    # Writing the evidence-only copy over the source would silently destroy
    # the case, decision and tenant planes -- the one part of the system that
    # cannot be rebuilt from public data. Nothing about `--out` makes that
    # obvious, so refuse it here rather than in the caller.
    if target.resolve() == source.resolve():
        raise ValueError(
            f"refusing to overwrite the working warehouse at {source}: "
            "the export drops the case, decision and tenant tables"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    partial.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    connection = duckdb.connect(str(partial))
    try:
        # ATTACH takes no bound parameter, so the path is a literal. It comes
        # from config rather than a request, and the doubled quote is the
        # standard escape -- a directory name with an apostrophe in it is not
        # exotic on a desktop, which is where this runs.
        literal = as_posix(source).replace("'", "''")
        connection.execute(f"ATTACH '{literal}' AS src (READ_ONLY)")
        for table in PUBLIC_TABLES:
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM src.{table}")
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        connection.execute("DETACH src")
    finally:
        connection.close()

    # Straight over the top: `replace` overwrites atomically on both POSIX and
    # Windows. Unlinking first opened a window where an interrupted export had
    # removed the previous artefact and not yet installed the new one.
    partial.replace(target)
    return counts


def loaded_panel(connection) -> str | None:
    """Which panel `assertion_span` currently holds.

    The single source of truth for this question. `snapshot_manifest`
    accumulates every panel ever ingested, so reading a panel or a coverage
    window from it unfiltered gives an answer belonging to whichever panel was
    ingested last -- which has now caused the same class of bug three times
    (slider stops, gene picker, report header). Ask here instead.
    """
    row = connection.execute(
        "SELECT any_value(panel) FROM assertion_span"
    ).fetchone()
    return row[0] if row else None


def coverage(connection, panel: str) -> tuple[str | None, str | None]:
    """Earliest and latest release ingested *for one panel*."""
    row = connection.execute(
        "SELECT MIN(release_date), MAX(release_date) FROM snapshot_manifest "
        "WHERE panel = ?",
        [panel],
    ).fetchone()
    return (str(row[0]) if row and row[0] else None,
            str(row[1]) if row and row[1] else None)


def release_dates(connection, panel: str) -> list[str]:
    """Every release date ingested for one panel, oldest first."""
    rows = connection.execute(
        "SELECT DISTINCT release_date FROM snapshot_manifest "
        "WHERE panel = ? ORDER BY release_date",
        [panel],
    ).fetchall()
    return [str(r[0]) for r in rows]


def state_on(connection, allele_id: str, as_of: str):
    """What the evidence said about one allele on one date."""
    return connection.execute(
        """
        SELECT bucket, stars, clnsig_raw, valid_from, valid_to
        FROM assertion_span
        WHERE allele_id = ?
          AND valid_from <= CAST(? AS DATE)
          AND valid_to   >  CAST(? AS DATE)
        """,
        [allele_id, as_of, as_of],
    ).fetchone()
