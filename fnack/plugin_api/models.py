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
    FingerprintResult,
    RecommendationItem,
    TaskResult,
    TrackRef,
)

# NOTE: DownloadResult is deliberately NOT re-exported from plugins.base.
# Phase 2 makes the SDK's DownloadResult the FINAL contract shape (below);
# legacy plugins keep importing the old class from plugins.base directly.
# The queue chain's migration adapter normalizes between the two.


# -- download.track ---------------------------------------------------------

@dataclass(frozen=True)
class DownloadRequest:
    track: TrackRef
    destination: Path
    quality: Optional[str] = None
    format: Optional[str] = None
    # Phase 2 (PR 4): provider-neutral hints used by search-based downloaders
    # (fnack.ytdlp). `query` overrides the title-based search string (e.g. a
    # raw YouTube/Deezer URL from the manual-download path). `cookies_path`
    # and `audio_source` are config hints; plugins fall back to their own
    # settings when absent. `check_duration` lets callers skip the provider's
    # internal duration verification (the queue verifies after download).
    query: Optional[str] = None
    cookies_path: Optional[str] = None
    audio_source: Optional[str] = None
    check_duration: bool = True


@dataclass
class DownloadResult:
    """FINAL SDK download result (Phase 2, MASTER §Verification).

    Provider-neutral: `provider_id` names WHO downloaded (never hardwired in
    core), `path` is the produced audio file, `error_code`/`message` are
    structured, `retryable` tells the DownloadService whether the next
    provider in the chain should be tried."""
    provider_id: str
    success: bool
    path: Optional[Path] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    retryable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


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
