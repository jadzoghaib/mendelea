"""Ingest mutual exclusion."""

import json
import os
import time

import pytest

from mendelea import locks


def test_lock_is_acquired_and_released(tmp_path):
    path = tmp_path / "x.lock"
    with locks.exclusive(path):
        assert path.exists()
    assert not path.exists()


def test_lock_records_the_holder(tmp_path):
    path = tmp_path / "x.lock"
    with locks.exclusive(path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["acquired_at"]


def test_second_holder_is_refused(tmp_path):
    """The mistake this exists to stop: a second ingest on the same panel."""
    path = tmp_path / "x.lock"
    with locks.exclusive(path):
        with pytest.raises(locks.LockHeld, match="held by"):
            with locks.exclusive(path):
                pass


def test_stale_lock_is_stolen(tmp_path):
    """A run killed mid-ingest must not block the panel forever."""
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": 999999, "acquired_at": "2020-01-01T00:00:00+00:00"}))
    old = time.time() - 10_000
    os.utime(path, (old, old))

    with locks.exclusive(path, stale_after=3600) as stolen_from:
        assert stolen_from is not None
        assert "999999" in stolen_from


def test_live_lock_is_not_stolen_before_the_deadline(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": 1, "acquired_at": "now"}))
    with pytest.raises(locks.LockHeld):
        with locks.exclusive(path, stale_after=3600):
            pass


def test_force_steals_a_live_lock(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": 1, "acquired_at": "now"}))
    with locks.exclusive(path, force=True) as stolen_from:
        assert stolen_from is not None


def test_lock_released_even_when_the_body_raises(tmp_path):
    path = tmp_path / "x.lock"
    with pytest.raises(ValueError):
        with locks.exclusive(path):
            raise ValueError("boom")
    assert not path.exists()


def test_corrupt_lock_file_is_still_respected(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("not json at all")
    with pytest.raises(locks.LockHeld, match="unknown holder"):
        with locks.exclusive(path, stale_after=3600):
            pass
