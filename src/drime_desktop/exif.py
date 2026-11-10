"""EXIF metadata extraction using exiftool."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ExifData:
    """Extracted EXIF metadata."""
    # Raw dict containing all EXIF fields - this is what we send to the API
    raw: dict[str, Any] | None = None
    
    # Parsed fields for convenience/checks
    date_taken: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """
        Convert to the format expected by the Drime API.
        
        Sends ALL extracted EXIF fields to preserve complete metadata.
        """
        if not self.raw:
            return {}
        
        result: dict[str, Any] = {}
        
        # Send everything, stripping the exiftool group prefix (e.g., "EXIF:", "File:")
        for key, value in self.raw.items():
            if value is None:
                continue
            # Skip empty strings
            if isinstance(value, str) and not value.strip():
                continue
            # Skip internal exiftool fields
            if key.startswith("ExifTool:") or key.startswith("System:"):
                continue
            # Skip file path/name for privacy
            if key in ("SourceFile", "File:FileName", "File:Directory"):
                continue
                
            # Strip the group prefix (EXIF:Make -> Make, Composite:GPSLatitude -> GPSLatitude)
            if ":" in key:
                api_key = key.split(":", 1)[1]
            else:
                api_key = key
            
            # Don't overwrite if we already have this field (prefer EXIF: over File:)
            if api_key not in result:
                result[api_key] = value
        
        # Add the signed lat/long as the web UI does
        if self.latitude is not None:
            result["latitude"] = self.latitude
        if self.longitude is not None:
            result["longitude"] = self.longitude
        
        return result

    @property
    def has_useful_data(self) -> bool:
        """True if we have date or location data worth syncing."""
        return self.date_taken is not None or (
            self.latitude is not None and self.longitude is not None
        )


def exiftool_available() -> bool:
    """Check if exiftool is installed."""
    return shutil.which("exiftool") is not None


def extract_exif(path: Path) -> ExifData | None:
    """
    Extract EXIF metadata from a file using exiftool.

    Returns None if extraction fails or no relevant data found.
    """
    if not path.exists():
        return None

    if not exiftool_available():
        return None

    try:
        result = subprocess.run(
            ["exiftool", "-json", "-n", "-G", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        if not data:
            return None

        tags = data[0]
        return _parse_exiftool_output(tags)

    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _parse_exiftool_output(tags: dict[str, Any]) -> ExifData:
    """Parse exiftool JSON output into ExifData."""
    exif = ExifData(raw=tags)

    # Parse date/time for the has_useful_data check
    date_fields = [
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "XMP:DateTimeOriginal",
        "XMP:CreateDate",
        "QuickTime:CreateDate",
        "QuickTime:MediaCreateDate",
    ]

    for field in date_fields:
        if field in tags and tags[field]:
            dt = _parse_datetime(tags[field])
            if dt:
                exif.date_taken = dt
                break

    # Parse GPS for the has_useful_data check and signed values
    lat = tags.get("EXIF:GPSLatitude") or tags.get("Composite:GPSLatitude")
    lon = tags.get("EXIF:GPSLongitude") or tags.get("Composite:GPSLongitude")

    if lat is not None and lon is not None:
        try:
            exif.latitude = float(lat)
            exif.longitude = float(lon)

            # Apply reference direction for signed values
            lat_ref = tags.get("EXIF:GPSLatitudeRef", "N")
            lon_ref = tags.get("EXIF:GPSLongitudeRef", "E")
            if lat_ref == "S":
                exif.latitude = -abs(exif.latitude)
            if lon_ref == "W":
                exif.longitude = -abs(exif.longitude)
        except (ValueError, TypeError):
            pass

    return exif


def _parse_datetime(value: str) -> datetime | None:
    """Parse various datetime formats from EXIF."""
    if not value or value.startswith("0000"):
        return None

    # Common EXIF formats
    formats = [
        "%Y:%m:%d %H:%M:%S",  # Standard EXIF
        "%Y:%m:%d %H:%M:%S%z",  # With timezone
        "%Y-%m-%dT%H:%M:%S",  # ISO
        "%Y-%m-%dT%H:%M:%S%z",  # ISO with timezone
        "%Y-%m-%dT%H:%M:%SZ",  # ISO UTC
    ]

    # Strip subseconds if present (e.g., "2024:01:15 10:30:00.123")
    if "." in value:
        value = value.split(".")[0]

    # Handle timezone offset in +00:00 format
    value = value.replace("+00:00", "+0000").replace("-00:00", "-0000")

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None
