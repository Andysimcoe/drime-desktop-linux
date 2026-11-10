"""Metadata sync - push EXIF data to Drime API for uploaded photos/videos.

This module handles syncing EXIF metadata (dates, GPS, camera info) for files
that were uploaded via the virtual drive. rclone uploads file content but not
embedded metadata, so Drime doesn't know the original capture date/location.

The sync process:
1. Queries Drime API for recent images/videos missing `captured_at`
2. Finds the local cached copy in ~/Drime
3. Extracts EXIF using exiftool
4. POSTs the metadata to the Drime API
"""
from __future__ import annotations

import configparser
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import backend
from .exif import ExifData, extract_exif, exiftool_available

logger = logging.getLogger(__name__)

# Constants
API_BASE = "https://app.drime.cloud/api/v1"
METADATA_SERVICE = "drime-metadata-sync.service"
METADATA_TIMER = "drime-metadata-sync.timer"
STATE_FILE = backend.CONFIG_DIR / "metadata-sync.json"

# Sync settings
DEFAULT_BATCH_SIZE = 50
FILE_TYPES = ["image", "video"]

# Scan windows
FAST_SCAN_WINDOW = timedelta(minutes=10)   # always look back at least this far on a quick run
FULL_SCAN_THRESHOLD = timedelta(minutes=20) # gap larger than this → scan from last_run, not just 10 min
DEFAULT_LOOKBACK = timedelta(days=7)        # first-ever run looks back this far


@dataclass
class MetadataState:
    """Persistent state for metadata sync."""
    last_run: int | None = None  # Unix timestamp
    files_processed: int = 0
    files_updated: int = 0

    @classmethod
    def load(cls) -> "MetadataState":
        if not STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(STATE_FILE.read_text())
            return cls(
                last_run=data.get("last_run"),
                files_processed=data.get("files_processed", 0),
                files_updated=data.get("files_updated", 0),
            )
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self) -> None:
        backend.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "last_run": self.last_run,
            "files_processed": self.files_processed,
            "files_updated": self.files_updated,
        }, indent=2))


@dataclass
class FileEntry:
    """A file entry from the Drime API."""
    id: int
    name: str
    type: str
    captured_at: str | None
    created_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileEntry":
        return cls(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            captured_at=data.get("captured_at"),
            created_at=data.get("created_at"),
        )

    @property
    def needs_metadata(self) -> bool:
        return self.captured_at is None


@dataclass
class SyncResult:
    """Result of a metadata sync run."""
    processed: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def get_api_token() -> str | None:
    """Read the Drime API token from rclone.conf."""
    rclone_conf = backend.CONFIG_HOME / "rclone" / "rclone.conf"
    if not rclone_conf.exists():
        return None
    config = configparser.ConfigParser()
    try:
        config.read(rclone_conf)
        if "drime" in config:
            return config["drime"].get("access_token")
    except configparser.Error:
        pass
    return None


def metadata_sync_problem() -> str | None:
    """Check if metadata sync can be enabled. Returns problem string or None."""
    if not exiftool_available():
        return f"exiftool is not installed ({backend.install_hint('perl-Image-ExifTool')})"
    if not get_api_token():
        return "Drime account not configured"
    return None


class DrimeAPIClient:
    """Simple client for Drime API metadata operations."""

    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        # Import requests lazily to avoid dependency issues
        import requests
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{API_BASE}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = self.session.post(f"{API_BASE}{path}", json=data, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def check_connection(self) -> bool:
        """Check if API is reachable."""
        try:
            self._get("/users/me")
            return True
        except Exception:
            return False

    def list_entries(
        self,
        file_type: str,
        per_page: int = 50,
        page: int = 1,
    ) -> tuple[list[FileEntry], int, int]:
        """List file entries. Returns (entries, current_page, last_page)."""
        params = {
            "workspaceId": 0,
            "perPage": per_page,
            "page": page,
            "type": file_type,
            "orderBy": "created_at",
            "orderDir": "desc",
        }
        data = self._get("/drive/file-entries", params)
        entries = [FileEntry.from_dict(e) for e in data.get("data", [])]
        return entries, data.get("current_page", 1), data.get("last_page", 1)

    def iter_recent_entries(self, since: datetime) -> Iterator[FileEntry]:
        """Iterate over image/video entries created after `since`, newest first.

        Stops fetching pages as soon as entries fall before the cutoff, so a
        fast scan (small window) only ever makes one or two API calls.
        """
        for file_type in FILE_TYPES:
            page = 1
            while True:
                entries, current, last = self.list_entries(file_type, DEFAULT_BATCH_SIZE, page)

                done = False
                for entry in entries:
                    # Entries are ordered by created_at desc - once we pass the
                    # cutoff we can stop paging entirely for this file type.
                    if entry.created_at:
                        try:
                            created = datetime.fromisoformat(
                                entry.created_at.replace("Z", "+00:00")
                            )
                            if created < since:
                                done = True
                                break
                        except ValueError:
                            pass
                    yield entry

                if done or current >= last or not entries:
                    break
                page += 1

    def set_metadata(self, entry_id: int, last_modified: int | None, exif_data: dict) -> bool:
        """Set metadata on a file entry."""
        payload: dict[str, Any] = {}
        if last_modified is not None:
            payload["lastModified"] = last_modified
        if exif_data:
            payload["exifData"] = exif_data
        if not payload:
            return False

        data = self._post(f"/file-entries/{entry_id}/metadata", payload)
        return data.get("updated", False)


def find_local_file(name: str) -> Path | None:
    """Find a file by name in the Drime mount or sync folder."""
    # Search both the virtual drive and the sync folder
    search_paths = [backend.MOUNT, backend.SYNC_DIR]
    
    for base_path in search_paths:
        if not base_path.exists():
            continue
        try:
            for path in base_path.rglob(name):
                if path.is_file():
                    return path
        except OSError:
            pass
    return None


def sync_entry(api: DrimeAPIClient, entry: FileEntry) -> tuple[bool, str | None]:
    """Sync metadata for a single entry. Returns (success, error_msg)."""
    local = find_local_file(entry.name)
    if not local:
        return False, f"Local file not found: {entry.name}"

    exif = extract_exif(local)
    if not exif or not exif.has_useful_data:
        return False, None  # Not an error, just no data

    exif_dict = exif.to_api_dict()
    last_modified = None
    if exif.date_taken:
        last_modified = int(exif.date_taken.timestamp() * 1000)

    try:
        if api.set_metadata(entry.id, last_modified, exif_dict):
            return True, None
        return False, "API returned updated=false"
    except Exception as e:
        return False, str(e)


def _scan_since(state: MetadataState) -> tuple[datetime, str]:
    """Calculate how far back to scan and return a human-readable label for logs.

    - First run ever           → full DEFAULT_LOOKBACK (7 days)
    - Gap since last run > 20m → scan from last_run (catch-up after downtime)
    - Recent run (< 20m ago)   → always scan at least FAST_SCAN_WINDOW (10 min)
      This covers the case where the timer fires every 2 minutes: a 10-minute
      window is wide enough to catch files whose API entry appears slightly
      after the local write (upload propagation delay).
    """
    now = datetime.now(timezone.utc)
    if state.last_run is None:
        return now - DEFAULT_LOOKBACK, f"first run, looking back {DEFAULT_LOOKBACK.days} days"
    last = datetime.fromtimestamp(state.last_run, tz=timezone.utc)
    gap = now - last
    if gap > FULL_SCAN_THRESHOLD:
        return last, f"catch-up scan from {last.strftime('%Y-%m-%d %H:%M')} UTC"
    return now - FAST_SCAN_WINDOW, "fast scan (last 10 minutes)"


def run_metadata_sync(log: backend.LogCb = backend._noop) -> SyncResult:
    """Run the metadata sync process."""
    result = SyncResult()

    token = get_api_token()
    if not token:
        log("No API token configured")
        return result

    # Check if at least one local source exists
    if not backend.MOUNT.exists() and not backend.SYNC_DIR.exists():
        log(f"No local files available (neither {backend.MOUNT} nor {backend.SYNC_DIR} exist)")
        return result

    api = DrimeAPIClient(token)
    if not api.check_connection():
        log("Cannot connect to Drime API")
        return result

    state = MetadataState.load()
    since, label = _scan_since(state)
    log(f"Starting metadata sync ({label})...")
    processed_ids: set[int] = set()

    for entry in api.iter_recent_entries(since):
        if entry.id in processed_ids:
            continue
        processed_ids.add(entry.id)
        result.processed += 1

        if not entry.needs_metadata:
            result.skipped += 1
            continue

        success, error = sync_entry(api, entry)
        if success:
            result.updated += 1
            log(f"Updated: {entry.name}")
        elif error:
            result.failed += 1
            result.errors.append(f"{entry.name}: {error}")

    # Update state
    state.last_run = int(time.time())
    state.files_processed += result.processed
    state.files_updated += result.updated
    state.save()

    log(f"Sync complete: {result.processed} checked, {result.updated} updated, "
        f"{result.skipped} skipped, {result.failed} failed")

    return result


# --- Metadata sync status (similar pattern to sync_status) ---

@dataclass
class MetadataSyncStatus:
    running: bool
    last_result: str
    last_start: int | None
    last_end: int | None
    next_run: int | None


def metadata_sync_status() -> MetadataSyncStatus:
    """Get metadata sync service status."""
    if backend.is_flatpak():
        # Flatpak uses daemon.py - would need to add metadata sync there
        from . import sandbox
        s = sandbox.read_status().get("metadata", {})
        alive = sandbox.daemon_alive()
        return MetadataSyncStatus(
            running=alive and bool(s.get("running")),
            last_result=s.get("last_result") or "",
            last_start=s.get("last_start"),
            last_end=s.get("last_end"),
            next_run=s.get("next_run") if alive else None,
        )

    out = backend.systemctl("show", METADATA_SERVICE, "--timestamp=unix", "-p",
                            "ActiveState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp").stdout
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    next_run = None
    lt = backend.run(["systemctl", "--user", "list-timers", METADATA_TIMER, "--output=json", "--all"])
    try:
        for t in json.loads(lt.stdout or "[]"):
            if t.get("unit") == METADATA_TIMER and t.get("next"):
                next_run = int(t["next"]) // 1_000_000
    except (ValueError, TypeError):
        pass
    return MetadataSyncStatus(
        running=props.get("ActiveState") in ("active", "activating"),
        last_result=props.get("Result", ""),
        last_start=backend._unix(props.get("ExecMainStartTimestamp", "")),
        last_end=backend._unix(props.get("ExecMainExitTimestamp", "")),
        next_run=next_run,
    )


def metadata_sync_log_tail(lines: int = 60) -> str:
    """Get recent log output from metadata sync."""
    if backend.is_flatpak():
        from . import sandbox
        # Filter the shared daemon log for metadata sync entries
        full = sandbox.log_tail(lines * 4)
        filtered = [l for l in full.splitlines()
                    if "[metadata]" in l or "metadata sync" in l.lower()]
        return "\n".join(filtered[-lines:])
    return backend.run(["journalctl", "--user", "-u", METADATA_SERVICE, "-n", str(lines),
                        "--no-pager", "-o", "short-iso"]).stdout
