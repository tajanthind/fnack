"""Domain models for the public SDK.

Phase 1.1 SDK-boundary review: `TrackRef`, `DownloadResult`, `FingerprintResult`
and the other re-exported classes below are TRANSITIONAL re-exports of
internal `plugins.base` classes — not yet standalone public contracts.
They are re-exported here (single canonical class) so plugins import from one
stable place; the debt is documented in docs/plugins/AUTHORING.md (§SDK
boundary) and paid down when Phase 2 moves provider implementations out of
core. The NEW models below (DownloadRequest, TrackResolveRequest,
FingerprintRequest/FingerprintEvidence, TrackCandidate) are real SDK
contracts defined here first.

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
# TRANSITIONAL (Phase 1.1 §4): these are internal-class re-exports, not yet
# standalone public contracts.
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
