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
