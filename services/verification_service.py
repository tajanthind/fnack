"""VerificationService — provider-neutral verification (Phase 3, MASTER §Verification).

Combines metadata evidence (tags, duration, ISRC) with fingerprint evidence
(from FingerprintService — AcoustID today, future providers) into a single
provider-neutral `VerificationResult`. Core policy: verification is core; the
providers only contribute NORMALIZED evidence — there is no provider-specific
rule here (no provider-specific match checks).

Fingerprint semantics (brief §Fingerprint semantics):
- provider no_match  -> no evidence (ignored)
- provider mismatch  -> negative evidence (lowers confidence)
- provider timeout  -> provider error (caller policy)
- provider unavailable -> CapabilityUnavailable (no provider) -> treated as
  no fingerprint evidence, NEVER as a mismatch

The result's `status`:
- "verified"    — metadata and/or strong fingerprint evidence matches.
- "mismatch"    — evidence shows a DIFFERENT track with high confidence.
- "uncertain"   — not enough evidence (missing tags, no fingerprint match);
                  NOT a mismatch.
- "provider_error" — a verification provider errored and no other evidence
                  decided; caller may retry or treat as uncertain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fnack.plugin_api.models import (
    FingerprintEvidence,
    MetadataEvidence,
    TrackMatch,
    TrackRef,
    VerificationResult,
)

# AcoustID "confirmed match" confidence gate (0.8) — the value the legacy
# chain used for the verify-when-unsure decision; kept as core verification
# policy (it is a confidence threshold on normalized evidence, not a
# provider-specific branch).
FINGERPRINT_CONFIRM_THRESHOLD = 0.8


class VerificationService:
    """Owns the provider-neutral verification policy."""

    def __init__(self, fingerprint_service=None, manager=None):
        self._fingerprint = fingerprint_service  # injectable for tests
        self._manager = manager

    def _fingerprint_service(self):
        if self._fingerprint is not None:
            return self._fingerprint
        from services.fingerprint_service import FingerprintService
        return FingerprintService(manager=self._manager)

    # -- metadata evidence --------------------------------------------------

    @staticmethod
    def _metadata_evidence(expected: TrackRef, file_path: Path) -> list[MetadataEvidence]:
        """Duration + embedded-tag evidence from the file (generic core
        helper services.verifier_service.verify_audio_file — the same check
        the pre-service chain used; it is provider-neutral)."""
        evidence: list[MetadataEvidence] = []
        try:
            from services.verifier_service import verify_audio_file
            ok, err, meta = verify_audio_file(
                file_path,
                expected_duration_seconds=expected.duration,
                expected_artist=expected.artist_name,
                expected_title=expected.title,
                max_duration_delta=8.0,
                delete_on_failure=False,
                check_tags=True,
            )
        except Exception as exc:
            return [MetadataEvidence(kind="file", status="unverifiable",
                                     detail=f"verifier error: {exc}")]
        if not ok and err:
            evidence.append(MetadataEvidence(kind="file", status="unverifiable",
                                             detail=err))
        else:
            evidence.append(MetadataEvidence(kind="file", status="match"))
        if meta.get("duration") is not None:
            actual = float(meta["duration"])
            expected_dur = expected.duration
            if expected_dur is not None and abs(actual - expected_dur) <= 8.0:
                evidence.append(MetadataEvidence(
                    kind="duration", status="match",
                    expected=expected_dur, actual=actual))
            elif expected_dur is None:
                evidence.append(MetadataEvidence(
                    kind="duration", status="unverifiable",
                    actual=actual, detail="no expected duration"))
            else:
                evidence.append(MetadataEvidence(
                    kind="duration", status="mismatch",
                    expected=expected_dur, actual=actual))
        if meta.get("tag_artist") or meta.get("tag_title"):
            expected_artist = (expected.artist_name or "").lower()
            expected_title = (expected.title or "").lower()
            tag_artist = (meta.get("tag_artist") or "").lower()
            tag_title = (meta.get("tag_title") or "").lower()
            if expected_title and tag_title:
                evidence.append(MetadataEvidence(
                    kind="tag_title",
                    status="match" if expected_title in tag_title or tag_title in expected_title
                    else "mismatch",
                    expected=expected.title, actual=meta.get("tag_title")))
            if expected_artist and tag_artist:
                evidence.append(MetadataEvidence(
                    kind="tag_artist",
                    status="match" if expected_artist in tag_artist or tag_artist in expected_artist
                    else "mismatch",
                    expected=expected.artist_name, actual=meta.get("tag_artist")))
        return evidence

    # -- decision -----------------------------------------------------------

    @staticmethod
    def _text_matches(a: Optional[str], b: Optional[str]) -> bool:
        if not a or not b:
            return False
        a = str(a).strip().lower()
        b = str(b).strip().lower()
        return a in b or b in a

    @staticmethod
    def _norm(s: Optional[str]) -> str:
        import re
        if not s:
            return ""
        return re.sub(r"[^a-zA-Z0-9]+", "", str(s)).lower()

    def _evidence_agrees(self, e: FingerprintEvidence, expected: TrackRef) -> bool:
        """Does the matched identity agree with the expected track? Faithful
        to the legacy candidate cross-check: EVERY present expected field
        (title, artist, duration) must be satisfied by the evidence; an ISRC
        equality also confirms. Absent fields are not evidence either way."""
        if e.isrc and expected.isrc and self._norm(e.isrc) == self._norm(expected.isrc):
            return True
        title_ok = not expected.title or (
            e.title and self._norm(e.title) == self._norm(expected.title))
        artist_ok = not expected.artist_name or (
            e.artist and self._norm(e.artist) == self._norm(expected.artist_name))
        duration_ok = True
        if expected.duration and e.duration:
            duration_ok = abs(float(e.duration) - float(expected.duration)) <= max(
                10.0, float(expected.duration) * 0.15)
        return title_ok and artist_ok and duration_ok

    def _decide(self, expected: TrackRef, metadata: list[MetadataEvidence],
                fingerprint: list[FingerprintEvidence]) -> VerificationResult:
        reasons: list[str] = []

        metadata_mismatch = any(e.status == "mismatch" for e in metadata
                                if e.kind in ("duration", "tag_title", "tag_artist"))
        metadata_match = any(e.status == "match" for e in metadata)
        unverifiable = any(e.status == "unverifiable" for e in metadata)

        # Fingerprint evidence is normalized (provider-neutral): each
        # provider reported what it matched (title/artist/confidence). The
        # SERVICE decides match vs mismatch by comparing that identity to the
        # expected track — no provider-specific branch (MASTER §Verification).
        found = [e for e in fingerprint if e.status == "match" and (e.confidence or 0) >= FINGERPRINT_CONFIRM_THRESHOLD]
        lower_confidence = [e for e in fingerprint if e.status == "match" and 0 < (e.confidence or 0) < FINGERPRINT_CONFIRM_THRESHOLD]
        errored = [e for e in fingerprint if e.status == "error"]

        agrees = []
        contradicts = []
        for e in found:
            if self._evidence_agrees(e, expected):
                agrees.append(e)
            elif e.title or e.artist:
                contradicts.append(e)

        canonical: Optional[TrackMatch] = None
        if agrees:
            best = max(agrees, key=lambda e: e.confidence or 0)
            canonical = TrackMatch(
                title=best.title, artist=best.artist, album=best.album,
                isrc=best.isrc, score=best.confidence or 0.0,
                provider_id=best.provider_id,
            )
            reasons.append(
                f"fingerprint match (confidence {best.confidence:.2f}) confirms the track")
            return VerificationResult(
                status="verified", score=best.confidence or 0.0,
                reasons=reasons,
                metadata_evidence=metadata, fingerprint_evidence=fingerprint,
                canonical_match=canonical)

        if contradicts:
            strongest = max(contradicts, key=lambda e: e.confidence or 0)
            reasons.append(
                f"fingerprint mismatch: provider matched a DIFFERENT track "
                f"({strongest.title or 'unknown'} by {strongest.artist or 'unknown'})")
            return VerificationResult(
                status="mismatch", score=0.0, reasons=reasons,
                metadata_evidence=metadata, fingerprint_evidence=fingerprint,
                canonical_match=TrackMatch(
                    title=strongest.title, artist=strongest.artist,
                    score=strongest.confidence or 0.0, provider_id=strongest.provider_id))

        # Lower-confidence fingerprint: accept only when the identity still
        # agrees with the expected track (the legacy verify-when-unsure gate).
        for e in lower_confidence:
            if self._evidence_agrees(e, expected):
                reasons.append(
                    f"fingerprint match (confidence {e.confidence:.2f}) confirms the track")
                return VerificationResult(
                    status="verified", score=e.confidence or 0.0,
                    reasons=reasons,
                    metadata_evidence=metadata, fingerprint_evidence=fingerprint,
                    canonical_match=TrackMatch(
                        title=e.title, artist=e.artist, isrc=e.isrc,
                        score=e.confidence or 0.0, provider_id=e.provider_id))

        if metadata_mismatch:
            mism = [e for e in metadata if e.status == "mismatch"]
            reasons.append("metadata mismatch: " + ", ".join(
                f"{e.kind} expected={e.expected!r} actual={e.actual!r}" for e in mism))
            # A strong metadata mismatch on title/artist/duration is treated as
            # a mismatch (tags were read and disagree) — the legacy chain
            # rejected confirmed tag mismatches the same way.
            return VerificationResult(
                status="mismatch", score=0.0, reasons=reasons,
                metadata_evidence=metadata, fingerprint_evidence=fingerprint)

        # Metadata-only confirmation (legacy chain semantics — the pre-service
        # verifier accepted on duration + embedded tags alone; the docstring
        # promises "metadata and/or strong fingerprint evidence matches").
        # Matching embedded tags PLUS a matching duration confirm the track
        # without a fingerprint. A duration match ALONE is not enough (the
        # audio could be a different song of the same length) — that stays
        # "no confirming evidence" below.
        tag_matches = [e for e in metadata
                       if e.status == "match" and e.kind in ("tag_title", "tag_artist")]
        duration_match = any(e.status == "match" and e.kind == "duration" for e in metadata)
        if tag_matches and (duration_match or expected.duration is None):
            reasons.append("metadata match (duration + tags) confirms the track")
            return VerificationResult(
                status="verified", score=1.0, reasons=reasons,
                metadata_evidence=metadata, fingerprint_evidence=fingerprint)

        # Provider error with no other deciding evidence -> provider_error
        # (caller may retry or treat as uncertain; NEVER a mismatch).
        if errored and not metadata_match:
            reasons.append("verification provider errored: " + ", ".join(
                e.error_code or "provider_error" for e in errored))
            return VerificationResult(
                status="provider_error", score=0.0, reasons=reasons,
                metadata_evidence=metadata, fingerprint_evidence=fingerprint)

        # Not enough evidence — uncertain, NOT a mismatch.
        if unverifiable and not metadata_match:
            reasons.append("metadata unverifiable (missing tags / duration)")
        else:
            reasons.append("no confirming evidence")
        return VerificationResult(
            status="uncertain", score=0.0, reasons=reasons,
            metadata_evidence=metadata, fingerprint_evidence=fingerprint)

    # -- verify -------------------------------------------------------------

    def verify(self, expected: TrackRef, file_path: Path) -> VerificationResult:
        """Verify a downloaded file against the expected track, combining
        metadata + fingerprint evidence. Provider-neutral — no acoustid/
        provider-specific branches in core.

        A missing fingerprint result (no provider / no_match) is never treated
        as proof of a mismatch: with no evidence the result is "uncertain".
        """
        metadata = self._metadata_evidence(expected, file_path)

        fingerprint: list[FingerprintEvidence] = []
        try:
            from fnack.plugin_api.models import FingerprintRequest
            fingerprint = self._fingerprint_service().identify(
                FingerprintRequest(file_path=file_path))
        except Exception:
            # No fingerprint provider (or provider failure) -> no fingerprint
            # evidence; never a mismatch on its own.
            fingerprint = []

        return self._decide(expected, metadata, fingerprint)
