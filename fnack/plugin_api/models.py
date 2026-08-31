"""Domain models for the public SDK.

Re-exports the existing, compatible models from `plugins.base` (single
source of truth — no duplicate domain classes) and adds the new typed
request/result models that don't exist there yet:

- `DownloadRequest`  — what a `download.track` provider is asked to do.
- `TrackResolveRequest` / `TrackCandidate` — `track.resolve`.
- `FingerprintRequest` / `FingerprintEvidence` — `fingerprint.identify`.

These are plain dataclasses, never SQLAlchemy rows. They cross the plugin
boundary in both directions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# Existing, already-working models — re-exported as-is (PHASE 1 §Public SDK:
# "Re-export existing compatible models instead of creating duplicate domain
# models unnecessarily"). Plugins and core keep using the same classes.
from plugins.base import (  # noqa: F401
    DownloadResult,
    FingerprintResult,
    RecommendationItem,
    TaskResult,
    TrackRef,
)


# -- download.track ---------------------------------------------------------

@dataclass(frozen=True)
class DownloadRequest:
    track: TrackRef
    destination: Path
    quality: Optional[str] = None
    format: Optional[str] = None


# -- track.resolve ----------------------------------------------------------

@dataclass(frozen=True)
class TrackResolveRequest:
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    isrc: Optional[str] = None
    duration: Optional[float] = None


@dataclass(frozen=True)
class TrackCandidate:
    provider_id: str
    external_id: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    isrc: Optional[str] = None
    duration: Optional[float] = None
    url: Optional[str] = None
    score: float = 0.0


# -- fingerprint.identify ---------------------------------------------------

@dataclass(frozen=True)
class FingerprintRequest:
    file_path: Path
    segment_seconds: Optional[float] = None


@dataclass
class FingerprintEvidence:
    """Normalized result of one fingerprint provider (MASTER §Verification).

    A provider `no_match` is NOT automatically a track mismatch — the
    VerificationService owns that decision (duration + metadata evidence are
    combined with this)."""
    provider_id: str
    status: str  # "match" | "no_match" | "mismatch" | "error" | "unsupported"
    confidence: Optional[float] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    isrc: Optional[str] = None
    duration: Optional[float] = None
    provider_track_id: Optional[str] = None
    retryable: bool = False
    error_code: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)
