"""Runtime configuration.

Everything the application needs to find state comes from here, and every
value can be overridden by an environment variable. That is not tidiness for
its own sake: it is what lets one container image run as shared SaaS, as a
dedicated per-tenant instance, or on-premise behind a hospital firewall, with
no code change. Nothing may be written next to the source tree.

Why the data directory defaults outside OneDrive
------------------------------------------------
The evidence plane grows to tens of gigabytes of immutable snapshots. Putting
that inside a synced OneDrive folder means every rebuild re-uploads the lot
and competes with the user's quota. Code lives in OneDrive; data does not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


DEFAULT_DATA_DIR = Path.home() / "mendelea-data"


def panel_dir() -> Path:
    """Where panel definitions live.

    Resolved rather than hardcoded, because two modules previously each
    computed it from a different parent depth -- cli.py used parents[2],
    web/server.py used parents[3]. Both happened to work from a source
    checkout and both would break on an installed package, where the panels
    are not beside the code at all. When that happens the gene picker silently
    stops filtering to the panel rather than failing, which is the kind of
    quiet regression worth spending a function to avoid.
    """
    override = os.environ.get("MENDELEA_PANEL_DIR")
    if override:
        return Path(override).expanduser()

    here = Path(__file__).resolve()
    for candidate in (
        here.parent / "panels",        # shipped inside the package
        here.parents[2] / "panels",    # source checkout: src/mendelea -> repo root
        Path.cwd() / "panels",
    ):
        if candidate.is_dir():
            return candidate
    return here.parents[2] / "panels"  # best guess, for the error message


@dataclass
class Config:
    """Resolved paths and knobs for one run."""

    data_dir: Path = field(default_factory=lambda: _env_path("MENDELEA_DATA_DIR", DEFAULT_DATA_DIR))
    assembly: str = os.environ.get("MENDELEA_ASSEMBLY", "GRCh38")
    http_timeout: int = int(os.environ.get("MENDELEA_HTTP_TIMEOUT", "120"))

    @property
    def snapshot_dir(self) -> Path:
        """Immutable, content-addressed evidence snapshots. Append-only."""
        return self.data_dir / "evidence" / "snapshots"

    @property
    def cache_dir(self) -> Path:
        """Reusable downloads (tabix indexes). Safe to delete; costs a refetch."""
        return self.data_dir / "cache"

    @property
    def reference_dir(self) -> Path:
        """Gene coordinates and other small reference data."""
        return self.data_dir / "reference"

    @property
    def warehouse(self) -> Path:
        """DuckDB file holding manifests and derived tables."""
        return self.data_dir / "mendelea.duckdb"

    # Note: the case and decision planes live in the warehouse, in tables
    # scoped by tenant_id, not in per-tenant directories. An earlier
    # `tenant_dir` here created an empty directory that implied a storage
    # layout which never existed -- removed rather than left to mislead.

    def ensure_dirs(self) -> None:
        for path in (self.snapshot_dir, self.cache_dir, self.reference_dir):
            path.mkdir(parents=True, exist_ok=True)


def load() -> Config:
    config = Config()
    config.ensure_dirs()
    return config
