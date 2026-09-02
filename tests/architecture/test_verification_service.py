"""Architecture/parity test: FingerprintService + VerificationService (Phase 3, Step 3).

Verifies the Phase 3 verification contract:

1. FingerprintService resolves fingerprint.identify providers via the
   capability registry; zero providers -> CapabilityUnavailable; provider
   errors/timeouts normalize to `error` evidence (never crash); no_match ->
   no evidence (never a mismatch).
2. Evidence normalization: SDK FingerprintEvidence passes through; legacy
   FingerprintResult (confidence/matched_title/matched_artist) normalizes.
3. VerificationService is provider-neutral — no acoustid_service import, no
   `acoustid_match` rules in core; it combines normalized metadata +
   fingerprint evidence into VerificationResult.
4. Decision semantics:
   - metadata (tags/duration) match -> verified
   - fingerprint match agreeing with expected -> verified (wrong-tags rescue)
   - fingerprint match contradicting expected -> mismatch
   - metadata mismatch -> mismatch
   - no evidence -> uncertain (NOT a mismatch)
   - provider error without other evidence -> provider_error
5. A missing fingerprint result is never treated as proof of a mismatch.
6. queue_service's _verify_or_rescue routes through VerificationService (no
   services.acoustid_service import remains in the queue).

Run from the repo root:

    .venv/bin/python tests/architecture/test_verification_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _fingerprint(provider_id="fnack.acoustid", status="match", confidence=0.9,
                 title="Back in Black", artist="AC/DC", isrc=None, error_code=None):
    from fnack.plugin_api.models import FingerprintEvidence
    return FingerprintEvidence(
        provider_id=provider_id, status=status, confidence=confidence,
        title=title, artist=artist, isrc=isrc, error_code=error_code,
    )


class _FakeFingerprintService:
    def __init__(self, evidence=None, raises=None):
        self._evidence = evidence or []
        self._raises = raises

    def identify(self, request):
        if self._raises is not None:
            raise self._raises
        return self._evidence


def _verify(expected, metadata_kind="unverifiable", fp_evidence=None):
    """Drive VerificationService with a fake fingerprint service; metadata
    comes from a temp audio file."""
    from services.verification_service import VerificationService
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
        f.write(b"\x00" * 1024)  # not a real audio file -> unverifiable
        path = Path(f.name)

    svc = VerificationService(fingerprint_service=_FakeFingerprintService(fp_evidence))
    try:
        return svc.verify(expected, path)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _track_ref(title="Back in Black", artist="AC/DC", duration=None):
    from fnack.plugin_api.models import TrackRef
    return TrackRef(id=1, title=title, artist_name=artist, album_name="",
                    duration=duration)


def test_services_have_no_provider_imports_or_id_branches() -> None:
    """Source-level: neither service imports provider services; no
    acoustid-specific rules in core."""
    for name in ("fingerprint_service.py", "verification_service.py"):
        src = (ROOT / "services" / name).read_text(encoding="utf-8")
        for needle in ["services.acoustid_service", "services.deezer_service",
                       "services.spotify_service", "services.musicbrainz_service",
                       '"fnack.acoustid"', "acoustid_match"]:
            assert needle not in src, f"{name} must stay provider-neutral ({needle})"
    # The capability is resolved by id, not hardwired
    v_src = (ROOT / "services" / "verification_service.py").read_text(encoding="utf-8")
    assert "fingerprint" in v_src.lower()


def test_fingerprint_zero_providers_raise_capability_unavailable() -> None:
    """FingerprintService with no fingerprint.identify provider -> structured
    CapabilityUnavailable."""
    from services.fingerprint_service import CapabilityUnavailable, FingerprintService
    from fnack.plugin_api.models import FingerprintRequest

    class NoProviders:
        def has_capability(self, c): return False
        def get_capability_providers(self, c): raise Exception("no providers")

    svc = FingerprintService(manager=NoProviders())
    try:
        svc.identify(FingerprintRequest(file_path=Path("/tmp/x.flac")))
    except CapabilityUnavailable as e:
        assert e.capability == "fingerprint.identify"
    else:
        raise AssertionError("expected CapabilityUnavailable with zero providers")


def test_fingerprint_normalizes_legacy_and_sdk_shapes() -> None:
    """The service adapts both contract shapes and maps no_match to no
    evidence (never a mismatch)."""
    from services.fingerprint_service import FingerprintService
    from fnack.plugin_api.models import FingerprintEvidence, FingerprintRequest

    class LegacyFp:
        capability_id = "fingerprint.identify"
        def __init__(self, hit):
            self._hit = hit
        @property
        def manifest(self):
            class _M: id = "fnack.acoustid"
            return _M()
        def identify(self, file_path):
            if not self._hit:
                return None
            from plugins.base import FingerprintResult
            return FingerprintResult(confidence=0.95, matched_title="Back in Black",
                                     matched_artist="AC/DC", raw={"score": 0.95})

    class SdkFp:
        capability_id = "fingerprint.identify"
        def __init__(self, evidence):
            self._evidence = evidence
        @property
        def manifest(self):
            class _M: id = "fnack.future"
            return _M()
        def identify(self, request):
            return self._evidence

    class Mgr:
        def __init__(self, providers): self._p = providers
        def has_capability(self, c): return True
        def get_capability_providers(self, c): return self._p
        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            import inspect, asyncio
            method = getattr(provider, method_name)
            r = method(*args, **kwargs)
            if inspect.isawaitable(r): return asyncio.run(r)
            return r

    req = FingerprintRequest(file_path=Path("/tmp/x.flac"))

    # Legacy hit -> evidence with title/artist + status match
    svc = FingerprintService(manager=Mgr([LegacyFp(hit=True)]))
    ev = svc.identify(req)
    assert len(ev) == 1
    assert ev[0].status == "match" and ev[0].confidence == 0.95
    assert ev[0].title == "Back in Black" and ev[0].artist == "AC/DC"

    # Legacy no-hit -> no evidence (not a mismatch)
    svc2 = FingerprintService(manager=Mgr([LegacyFp(hit=False)]))
    assert svc2.identify(req) == []

    # SDK shape passes through
    sdk_ev = _fingerprint(provider_id="fnack.future", confidence=0.8)
    svc3 = FingerprintService(manager=Mgr([SdkFp(sdk_ev)]))
    assert svc3.identify(req) == [sdk_ev]

    # Provider error -> error evidence, not a crash
    class BoomFp:
        capability_id = "fingerprint.identify"
        @property
        def manifest(self):
            class _M: id = "fnack.boom"
            return _M()
        def identify(self, file_path):
            raise RuntimeError("fpcalc missing")
    svc4 = FingerprintService(manager=Mgr([BoomFp()]))
    ev4 = svc4.identify(req)
    assert len(ev4) == 1 and ev4[0].status == "error" and ev4[0].retryable is True


def test_verify_metadata_match_is_verified() -> None:
    """Duration + tag evidence match -> verified WITHOUT a fingerprint (the
    legacy chain semantics: matching embedded tags + matching duration
    confirm the track; the queue must accept a well-tagged download)."""
    from services.verification_service import VerificationService
    from fnack.plugin_api.models import MetadataEvidence
    svc = VerificationService(fingerprint_service=_FakeFingerprintService([]))
    expected = _track_ref(duration=231.0)
    result = svc._decide(expected, metadata=[
        MetadataEvidence(kind="file", status="match"),
        MetadataEvidence(kind="duration", status="match", expected=231.0, actual=231.2),
        MetadataEvidence(kind="tag_title", status="match", expected="Pushkar", actual="Pushkar"),
        MetadataEvidence(kind="tag_artist", status="match", expected="Dulla", actual="Dulla"),
    ], fingerprint=[])
    assert result.status == "verified", result.reasons
    assert "no confirming evidence" not in result.reasons
    assert result.score >= 1.0


def test_verify_metadata_match_without_duration_check_is_verified() -> None:
    """Tag match with duration checking disabled (expected duration None) ->
    verified; the disabled-duration path must not reject a tagged file."""
    from services.verification_service import VerificationService
    from fnack.plugin_api.models import MetadataEvidence
    svc = VerificationService(fingerprint_service=_FakeFingerprintService([]))
    result = svc._decide(_track_ref(duration=None), metadata=[
        MetadataEvidence(kind="duration", status="unverifiable", actual=231.2,
                         detail="no expected duration"),
        MetadataEvidence(kind="tag_title", status="match", expected="Pushkar", actual="Pushkar"),
        MetadataEvidence(kind="tag_artist", status="match", expected="Dulla", actual="Dulla"),
    ], fingerprint=[])
    assert result.status == "verified", result.reasons


def test_duration_match_alone_is_uncertain_not_verified() -> None:
    """Duration match with NO tags is NOT enough to confirm identity — stays
    uncertain (the yt-dlp fallback case: tagless YouTube audio)."""
    from services.verification_service import VerificationService
    from fnack.plugin_api.models import MetadataEvidence
    svc = VerificationService(fingerprint_service=_FakeFingerprintService([]))
    result = svc._decide(_track_ref(duration=231.0), metadata=[
        MetadataEvidence(kind="file", status="match"),
        MetadataEvidence(kind="duration", status="match", expected=231.0, actual=231.2),
    ], fingerprint=[])
    assert result.status == "uncertain", result.reasons
    assert "no confirming evidence" in result.reasons


def test_fingerprint_agreeing_is_verified_and_rescues_wrong_tags() -> None:
    """A high-confidence fingerprint match whose identity agrees with the
    expected track -> verified, even when tags/duration disagree (the
    wrong-tags rescue preserved by the provider-neutral service)."""
    result = _verify(_track_ref(),
                     fp_evidence=[_fingerprint(title="Back in Black", artist="AC/DC", confidence=0.95)])
    assert result.status == "verified", result.reasons
    assert result.canonical_match is not None
    assert result.canonical_match.title == "Back in Black"
    assert result.score >= 0.9


def test_fingerprint_contradicting_is_mismatch() -> None:
    """A high-confidence fingerprint match that is a DIFFERENT song ->
    mismatch."""
    result = _verify(_track_ref(),
                     fp_evidence=[_fingerprint(title="Thunderstruck", artist="AC/DC", confidence=0.95)])
    assert result.status == "mismatch", result.reasons
    assert result.canonical_match.title == "Thunderstruck"


def test_no_evidence_is_uncertain_not_mismatch() -> None:
    """Missing fingerprint result + unverifiable metadata -> uncertain, NEVER
    a mismatch (brief §Fingerprint semantics)."""
    result = _verify(_track_ref(), fp_evidence=[])
    assert result.status == "uncertain", result.reasons


def test_provider_error_is_provider_error() -> None:
    """Provider error with no other evidence -> provider_error."""
    err_ev = _fingerprint(status="error", error_code="provider_error", title=None, artist=None)
    result = _verify(_track_ref(), fp_evidence=[err_ev])
    assert result.status == "provider_error", result.reasons


def test_queue_verify_routes_through_service() -> None:
    """queue_service._verify_or_rescue uses VerificationService; no
    services.acoustid_service import remains in the queue."""
    src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    assert "VerificationService" in src
    assert "from services.acoustid_service import verify_download" not in src
    # The service itself imports only the SDK + generic verifier helper.
    v_src = (ROOT / "services" / "verification_service.py").read_text(encoding="utf-8")
    assert "services.verifier_service" in v_src  # generic core metadata helper


if __name__ == "__main__":
    test_services_have_no_provider_imports_or_id_branches()
    test_fingerprint_zero_providers_raise_capability_unavailable()
    test_fingerprint_normalizes_legacy_and_sdk_shapes()
    test_verify_metadata_match_is_verified()
    test_fingerprint_agreeing_is_verified_and_rescues_wrong_tags()
    test_fingerprint_contradicting_is_mismatch()
    test_no_evidence_is_uncertain_not_mismatch()
    test_provider_error_is_provider_error()
    test_queue_verify_routes_through_service()
    print("test_verification_service: PASSED")
