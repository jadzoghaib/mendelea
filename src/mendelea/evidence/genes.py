"""Gene symbol to genomic region resolution.

A range query needs coordinates before it can ask for a gene, so this is the
one lookup that cannot itself be range-queried.

Two providers, tried in order, because a single upstream outage must not be
able to stop an ingest -- Ensembl returned 500s throughout the first build of
this module, which is exactly the failure this structure absorbs. Results are
cached permanently: gene boundaries on a fixed assembly do not move, and a
bundled cache ships with the repository so the standard panels resolve with no
network call at all.

Assembly safety
---------------
Silently resolving against the wrong build is the dangerous failure here: the
coordinates would look plausible, the range query would succeed, and it would
return the wrong region of the genome. Providers are therefore required to
state their assembly, and `verify_regions` cross-checks resolved regions
against the gene symbols ClinVar itself reports in that region.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import requests

# Gene boundaries exclude regulatory and UTR context that ClinVar sometimes
# annotates to the gene; pad so those records are not silently dropped.
FLANK_BP = 5000


@dataclass(frozen=True)
class GeneRegion:
    symbol: str
    contig: str
    start: int
    end: int
    source: str = "cache"

    def padded(self, flank: int = FLANK_BP) -> tuple[str, int, int]:
        return self.contig, max(1, self.start - flank), self.end + flank


class GeneResolutionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def _from_ensembl(symbol: str, session: requests.Session, assembly: str) -> dict | None:
    response = session.get(
        f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{symbol}",
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if not response.ok:
        return None
    payload = response.json()
    if payload.get("assembly_name") != assembly:
        return None
    return {
        "contig": str(payload["seq_region_name"]),
        "start": int(payload["start"]),
        "end": int(payload["end"]),
        "source": "ensembl",
    }


def _from_mygene(symbol: str, session: requests.Session, assembly: str) -> dict | None:
    """MyGene.info. Its default `genomic_pos` is GRCh38/hg38."""
    if assembly != "GRCh38":
        return None

    response = session.get(
        "https://mygene.info/v3/query",
        params={
            "q": f"symbol:{symbol}",
            "species": "human",
            "fields": "symbol,genomic_pos",
            "size": 1,
        },
        timeout=30,
    )
    if not response.ok:
        return None

    hits = response.json().get("hits") or []
    if not hits:
        return None

    position = hits[0].get("genomic_pos")
    # Genes on patch scaffolds come back as a list; take the primary assembly.
    if isinstance(position, list):
        position = next(
            (p for p in position if len(str(p.get("chr", ""))) <= 2), position[0]
        )
    if not position:
        return None

    return {
        "contig": str(position["chr"]),
        "start": int(position["start"]),
        "end": int(position["end"]),
        "source": "mygene",
    }


PROVIDERS = (_from_ensembl, _from_mygene)


# --------------------------------------------------------------------------


class GeneResolver:
    # A provider that has failed this many times in a row is treated as down
    # for the rest of the run. Without this, a single dead upstream costs
    # (retries x backoff) on *every* gene -- 31 genes against an unreachable
    # Ensembl took longer than the entire ingest it was meant to precede.
    FAILURE_BUDGET = 2

    def __init__(self, cache_path: Path, bundled: Path | None = None,
                 session: requests.Session | None = None, assembly: str = "GRCh38"):
        self.cache_path = cache_path
        self.assembly = assembly
        self.session = session or requests.Session()
        self._cache: dict[str, dict] = {}
        self._failures: dict[str, int] = {}

        for path in (bundled, cache_path):
            if path and path.exists():
                try:
                    self._cache.update(json.loads(path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass

    def _persist(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8"
        )

    def resolve(self, symbol: str) -> GeneRegion:
        symbol = symbol.upper()
        if symbol in self._cache:
            entry = self._cache[symbol]
            return GeneRegion(
                symbol, entry["contig"], entry["start"], entry["end"],
                entry.get("source", "cache"),
            )

        errors = []
        for provider in PROVIDERS:
            name = provider.__name__
            if self._failures.get(name, 0) >= self.FAILURE_BUDGET:
                errors.append(f"{name}: skipped, marked down for this run")
                continue

            entry = None
            for attempt in range(2):
                try:
                    entry = provider(symbol, self.session, self.assembly)
                except Exception as exc:  # noqa: BLE001 - try the next provider
                    errors.append(f"{name}: {exc}")
                    break
                if entry or attempt:
                    break
                time.sleep(1)

            if entry:
                self._failures[name] = 0
                self._cache[symbol] = entry
                self._persist()
                return GeneRegion(
                    symbol, entry["contig"], entry["start"], entry["end"],
                    entry["source"],
                )

            self._failures[name] = self._failures.get(name, 0) + 1
            errors.append(f"{name}: no usable answer")

        raise GeneResolutionError(f"could not resolve {symbol}: {'; '.join(errors)}")

    def resolve_all(self, symbols: list[str]) -> list[GeneRegion]:
        return [self.resolve(s) for s in symbols]


def verify_regions(regions: list[GeneRegion], observed: dict[str, set[str]],
                   min_concordance: float = 0.5) -> list[str]:
    """Cross-check resolved regions against the gene symbols ClinVar reports there.

    If coordinates came from the wrong assembly the range query still succeeds
    -- it just returns the wrong part of the genome. The cheap tell is that
    ClinVar's own GENEINFO for those records names a different gene. Returns a
    list of human-readable warnings; empty means everything agreed.
    """
    warnings = []
    for region in regions:
        symbols = observed.get(region.symbol, set())
        if not symbols:
            warnings.append(f"{region.symbol}: no ClinVar records in resolved region")
        elif region.symbol not in symbols:
            warnings.append(
                f"{region.symbol}: resolved region reports {sorted(symbols)[:3]} "
                f"instead -- possible wrong assembly"
            )
    return warnings


def load_panel(path: Path) -> tuple[str, list[str]]:
    """Read a panel file: {"name": ..., "genes": [...]}."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["name"], [g.upper() for g in payload["genes"]]
