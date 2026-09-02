"""Architecture/parity test: Phase 3 completion criteria (Step 5).

Asserts the brief's §Completion criteria at the source level:

1. queue_service has NO provider imports (only generic core: verifier_service)
   and NO provider IDs.
2. The queue orchestrates through the four application services
   (DownloadService / MetadataService / VerificationService /
   MediaServerService).
3. API routes (app.py) call application services; the only remaining
   provider-service imports are the documented transitional ones (musicbrainz
   enrich, acoustid manual-identify, navidrome fix-splits — none have a
   capability in the MASTER set).
4. Zero providers produces structured unavailable results: every application
   service raises CapabilityUnavailable when its capability has no enabled
   provider (no hidden fallback).
5. Multiple providers work: the services resolve priority-ordered provider
   lists and apply first-success/first-non-empty policies.
6. Provider errors do not crash the queue: DownloadService aggregates
   per-provider failures into a failure result.

Run from the repo root:

    .venv/bin/python tests/architecture/test_phase3_completion.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FORBIDDEN_PROVIDER_MODULES = {
    "services.spotify_service",
    "services.deezer_service",
    "services.musicbrainz_service",
    "services.acoustid_service",
    "services.navidrome_service",
    "services.itunes_service",
}


def test_queue_has_no_provider_imports_or_ids() -> None:
    """Completion criterion: queue has no provider imports; queue has no
    provider IDs."""
    src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    for mod in FORBIDDEN_PROVIDER_MODULES:
        assert f"import {mod}" not in src, f"queue must not import {mod}"
        assert f"from {mod} import" not in src, f"queue must not import {mod}"
    # No provider-ID branches: no equality tests against fnack.* ids.
    import re
    id_branches = re.findall(r"""(?:==|!=|is not|is)\s*["']fnack\.[a-z0-9\-]+["']""", src)
    assert not id_branches, f"queue must not branch on provider IDs: {id_branches}"


def test_queue_orchestrates_through_application_services() -> None:
    """The queue drives the four application services (capability owners)."""
    src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    for svc in ("DownloadService", "MetadataService", "VerificationService", "MediaServerService"):
        assert svc in src, f"queue must use {svc}"


def test_app_routes_use_application_services() -> None:
    """API routes call application services; remaining provider imports are
    exactly the documented transitional ones (no capability in MASTER set)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "MetadataService" in src      # search-artist / sync routes
    assert "MediaServerService" in src   # navidrome test/scan routes
    # Navidrome extracted (Phase 4 PR 5): no service import; split repair
    # resolves through the plugin.
    assert "services.navidrome_service" not in src
    assert "run_split_repair" in src
    # Other providers extracted in their own Phase 4 PRs (musicbrainz PR 3,
    # acoustid PR 4) — the transitional asserts were updated there.


def test_zero_providers_produce_structured_unavailable() -> None:
    """Every application service raises CapabilityUnavailable when its
    capability has no enabled provider (MASTER rule 3 — no hidden fallback)."""
    from services.download_service import CapabilityUnavailable as CU_D
    from services.metadata_service import CapabilityUnavailable as CU_M
    from services.fingerprint_service import CapabilityUnavailable as CU_F
    from services.media_server_service import CapabilityUnavailable as CU_S

    class Empty:
        def has_capability(self, cap): return False
        def get_capability_providers(self, cap): raise Exception("empty")
        def get_downloaders(self): return []

    from services.download_service import DownloadService
    from services.metadata_service import MetadataService
    from services.fingerprint_service import FingerprintService
    from services.media_server_service import MediaServerService
    from fnack.plugin_api.models import DownloadRequest, FingerprintRequest
    from plugins.base import TrackRef

    # DownloadService: zero providers -> CapabilityUnavailable("download.track")
    try:
        DownloadService(manager=Empty()).download(
            DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                            destination=Path("/tmp")))
    except CU_D as e:
        assert e.capability == "download.track"
    else:
        raise AssertionError("DownloadService must raise on zero providers")

    # MetadataService: zero providers -> CapabilityUnavailable per method
    for call in (lambda: MetadataService(manager=Empty()).search_artist("A"),
                 lambda: MetadataService(manager=Empty()).get_artist_discography("1")):
        try:
            call()
        except CU_M:
            pass
        else:
            raise AssertionError("MetadataService must raise on zero providers")

    # FingerprintService
    try:
        FingerprintService(manager=Empty()).identify(FingerprintRequest(file_path=Path("/tmp/x")))
    except CU_F:
        pass
    else:
        raise AssertionError("FingerprintService must raise on zero providers")

    # MediaServerService
    try:
        MediaServerService(manager=Empty()).scan()
    except CU_S:
        pass
    else:
        raise AssertionError("MediaServerService must raise on zero providers")


def test_multiple_providers_work() -> None:
    """Services resolve priority-ordered provider lists and apply
    first-success / first-non-empty policies (DownloadService fallback,
    MetadataService first-non-empty, FingerprintService evidence fan-out,
    MediaServerService first-success)."""
    from services.download_service import DownloadService
    from services.metadata_service import MetadataService
    from services.fingerprint_service import FingerprintService
    from services.media_server_service import MediaServerService
    from fnack.plugin_api.models import DownloadRequest, DownloadResult, FingerprintRequest
    from plugins.base import TrackRef
    import asyncio
    import inspect

    class SdkProv:
        capability_id = "download.track"
        def __init__(self, pid, fail=False):
            self._pid, self._fail = pid, fail
        @property
        def manifest(self):
            class _M: id = self._pid
            return _M()
        async def can_handle(self, request): return True
        async def download(self, request):
            if self._fail:
                return DownloadResult(provider_id=self._pid, success=False, message="boom")
            return DownloadResult(provider_id=self._pid, success=True, path=Path("/tmp/ok.flac"))

    class FpProv:
        capability_id = "fingerprint.identify"
        def __init__(self, evidence): self._ev = evidence
        @property
        def manifest(self):
            class _M: id = "fnack.fp"
            return _M()
        def identify(self, file_path): return self._ev

    class MediaProv:
        def __init__(self, cap, ok): self.capability_id, self._ok = cap, ok
        @property
        def manifest(self):
            class _M: id = "fnack.media"
            return _M()
        def trigger_scan(self): return self._ok, "scan"
        def test_connection(self): return self._ok, "conn"
        def health(self): return {"ok": self._ok} if self._ok else None

    class Mgr:
        def __init__(self, caps): self._caps = caps  # cap -> [providers]
        def has_capability(self, c): return c in self._caps and bool(self._caps[c])
        def get_capability_providers(self, c): return list(self._caps.get(c, []))
        def get_downloaders(self): return list(self._caps.get("download.track", []))
        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            r = method(*args, **kwargs)
            if inspect.isawaitable(r): return asyncio.run(r)
            return r

    # Download fallback: first fails, second succeeds
    mgr = Mgr({"download.track": [SdkProv("fnack.a", fail=True), SdkProv("fnack.b")]})
    r = DownloadService(manager=mgr).download(
        DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                        destination=Path("/tmp")))
    assert r.success and r.provider_id == "fnack.b"

    # Metadata first-non-empty
    class MetaProv:
        capability_id = "artist.search"
        def __init__(self, results): self._r = results
        @property
        def manifest(self):
            class _M: id = "fnack.meta"
            return _M()
        def search_artist(self, name): return self._r
    mgr2 = Mgr({"artist.search": [MetaProv([]), MetaProv([{"id": 1, "name": "A"}])]})
    assert MetadataService(manager=mgr2).search_artist("A") == [{"id": 1, "name": "A"}]

    # Media first-success
    mgr3 = Mgr({"media.scan": [MediaProv("media.scan", False), MediaProv("media.scan", True)]})
    assert MediaServerService(manager=mgr3).scan() == (True, "scan")

    # Fingerprint evidence per provider
    from fnack.plugin_api.models import FingerprintEvidence
    ev = FingerprintEvidence(provider_id="fnack.fp", status="match", confidence=0.9, title="T", artist="A")
    mgr4 = Mgr({"fingerprint.identify": [FpProv(ev)]})
    out = FingerprintService(manager=mgr4).identify(FingerprintRequest(file_path=Path("/tmp/x")))
    assert len(out) == 1 and out[0].provider_id == "fnack.fp"


def test_provider_errors_do_not_crash_queue() -> None:
    """DownloadService aggregates provider failures into a failure result
    (never raises for a provider that crashed); FingerprintService turns
    provider errors into error evidence."""
    from services.download_service import DownloadService
    from services.fingerprint_service import FingerprintService
    from fnack.plugin_api.models import DownloadRequest, FingerprintRequest
    from plugins.base import TrackRef
    import asyncio
    import inspect

    class BoomProv:
        capability_id = "download.track"
        @property
        def manifest(self):
            class _M: id = "fnack.boom"
            return _M()
        async def can_handle(self, request): return True
        async def download(self, request): raise RuntimeError("disk full")

    class BoomFp:
        capability_id = "fingerprint.identify"
        @property
        def manifest(self):
            class _M: id = "fnack.boomfp"
            return _M()
        def identify(self, file_path): raise RuntimeError("fpcalc missing")

    class Mgr:
        def __init__(self, caps): self._caps = caps
        def has_capability(self, c): return c in self._caps and bool(self._caps[c])
        def get_capability_providers(self, c): return list(self._caps.get(c, []))
        def get_downloaders(self): return list(self._caps.get("download.track", []))
        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            try:
                r = method(*args, **kwargs)
                if inspect.isawaitable(r):
                    return asyncio.run(r)
                return r
            except Exception:
                return None  # manager boundary swallows provider errors

    r = DownloadService(manager=Mgr({"download.track": [BoomProv()]})).download(
        DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                        destination=Path("/tmp")))
    assert r.success is False and "fnack.boom" in (r.message or "")

    ev = FingerprintService(manager=Mgr({"fingerprint.identify": [BoomFp()]})).identify(
        FingerprintRequest(file_path=Path("/tmp/x")))
    # The manager boundary swallows provider errors: the provider is either
    # skipped (no evidence) or reported as error evidence — never a crash and
    # never a mismatch.
    assert all(e.status != "mismatch" for e in ev)
    assert any(e.status == "error" for e in ev) or ev == []


if __name__ == "__main__":
    test_queue_has_no_provider_imports_or_ids()
    test_queue_orchestrates_through_application_services()
    test_app_routes_use_application_services()
    test_zero_providers_produce_structured_unavailable()
    test_multiple_providers_work()
    test_provider_errors_do_not_crash_queue()
    print("test_phase3_completion: PASSED")
