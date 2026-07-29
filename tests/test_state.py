"""State file tests."""

from __future__ import annotations

import json

from glia_shopify_sync.state import State


def test_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    assert state.get_backfill_cursor() is None

    state.set_backfill_cursor("CURSOR_X", processed_at="2026-07-28T17:18:22Z")
    assert state.get_backfill_cursor() == "CURSOR_X"

    reloaded = State(path)
    assert reloaded.get_backfill_cursor() == "CURSOR_X"
    data = reloaded.load()
    assert data["backfill"]["last_processed_at"] == "2026-07-28T17:18:22Z"


def test_state_daily_run_round_trip(tmp_path):
    state = State(tmp_path / "state.json")
    assert state.get_daily_last_run() is None
    state.set_daily_last_run("2026-07-29T00:00:00Z")
    assert State(tmp_path / "state.json").get_daily_last_run() == "2026-07-29T00:00:00Z"


def test_state_corrupt_file_recovered(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not valid json", encoding="utf-8")

    state = State(path)
    # Returns defaults instead of raising; corrupt file backed up alongside.
    assert state.get_backfill_cursor() is None
    assert any(p.name.startswith("state.json.corrupt.") for p in tmp_path.iterdir())


def test_state_preserves_unknown_keys(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"backfill": {"last_cursor": "C"}, "custom": {"note": "hi"}}),
        encoding="utf-8",
    )
    data = State(path).load()
    assert data["custom"] == {"note": "hi"}
