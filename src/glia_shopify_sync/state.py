"""JSON-backed pipeline state: backfill cursor + last daily-run timestamp.

The state file is small and human-readable so it can be inspected/edited. It is
written atomically (temp file + replace) and a corrupt file is backed up rather
than clobbered, mirroring the sibling erpnext-bank-integration project.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

_DEFAULT: dict[str, Any] = {
    "backfill": {
        # Shopify cursor to resume from (newest-first ordering); null = start.
        "last_cursor": None,
        # ISO timestamp of the most recent processedAt we've ingested.
        "last_processed_at": None,
    },
    "daily": {
        # ISO timestamp (UTC) of the last successful daily run.
        "last_run_at": None,
    },
}


class State:
    """Read/write the pipeline state JSON."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _deep_copy_default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            backup = self.path.with_suffix(self.path.suffix + f".corrupt.{_now_suffix()}.json")
            try:
                os.replace(self.path, backup)
                log.warning("state_corrupt_backed_up", backup=str(backup), error=str(e))
            except OSError:
                log.warning("state_corrupt_no_backup", error=str(e))
            return _deep_copy_default()
        return _merge_defaults(data)

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".", dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    # --- convenience accessors -----------------------------------------

    def get_backfill_cursor(self) -> str | None:
        return self.load().get("backfill", {}).get("last_cursor")

    def set_backfill_cursor(self, cursor: str | None, processed_at: str | None = None) -> None:
        data = self.load()
        data.setdefault("backfill", {})
        data["backfill"]["last_cursor"] = cursor
        if processed_at is not None:
            data["backfill"]["last_processed_at"] = processed_at
        self.save(data)

    def get_daily_last_run(self) -> str | None:
        return self.load().get("daily", {}).get("last_run_at")

    def set_daily_last_run(self, ts: str) -> None:
        data = self.load()
        data.setdefault("daily", {})["last_run_at"] = ts
        self.save(data)

    # --- incremental-sync cursor (backend-agnostic protocol) ------------

    def get_cursor(self) -> str | None:
        return self.get_backfill_cursor()

    def set_cursor(self, processed_at: str | None) -> None:
        self.set_backfill_cursor(None, processed_at=processed_at)


class FrappeState:
    """Stateless cursor store backed by the `Glia Sync State` singleton in ERPNext.

    Lets the daily CronJob pod be stateless (no PVC): it reads/writes the
    incremental-sync cursor via the Frappe REST API. Implements the same
    get_cursor/set_cursor protocol as `State`.
    """

    SINGLETON = "Glia Sync State"

    def __init__(self, frappe) -> None:
        self.frappe = frappe

    def get_cursor(self) -> str | None:
        from .frappe_client import FrappeError

        try:
            v = self.frappe.get_value(self.SINGLETON, self.SINGLETON, "last_processed_at")
        except FrappeError:
            return None
        if isinstance(v, dict):
            v = v.get("last_processed_at")
        return v or None

    def set_cursor(self, processed_at: str | None) -> None:
        self.frappe.set_value(self.SINGLETON, self.SINGLETON, {"last_processed_at": processed_at})


# --- helpers --------------------------------------------------------------


def _deep_copy_default() -> dict[str, Any]:
    return json.loads(json.dumps(_DEFAULT))


def _merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_copy_default()
    for section in ("backfill", "daily"):
        if isinstance(data.get(section), dict):
            merged[section].update(data[section])
    # Allow unknown extra keys to pass through (forward-compat).
    for k, v in data.items():
        if k not in merged:
            merged[k] = v
    return merged


def _now_suffix() -> str:
    import time

    return str(int(time.time()))


__all__ = ["FrappeState", "State"]
