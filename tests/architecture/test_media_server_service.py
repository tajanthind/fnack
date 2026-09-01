"""Architecture/parity test: MediaServerService (Phase 3, Step 4).

Verifies the Phase 3 media application-service contract:

1. MediaServerService resolves media.scan / media.health /
   media.connection_test via the capability registry — no navidrome import,
   no provider-ID branch.
2. Zero enabled providers -> CapabilityUnavailable per method.
3. scan / test_connection: first provider returning a usable result wins.
4. Candidate configuration: test_connection(candidate_config) forwards
   UNSAVED settings to providers that accept them (signature inspection) —
   the settings UI can validate a typed-but-not-saved config through the
   application service (brief §Candidate configuration).
5. Callers migrated: app.py navidrome test/scan routes and queue_service
   auto-scans use MediaServerService (no direct services.navidrome_service
   imports for scan/test in those files).

Run from the repo root:

    .venv/bin/python tests/architecture/test_media_server_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _fake_manager(*providers):
    import asyncio
    import inspect
    from collections import defaultdict

    caps = defaultdict(list)
    for p in providers:
        if getattr(p, "capability_id", None):
            caps[p.capability_id].append(p)

    class _FakeManager:
        def has_capability(self, capability):
            return capability in caps and bool(caps[capability])

        def get_capability_providers(self, capability):
            if capability not in caps or not caps[capability]:
                raise Exception("no providers")
            return list(caps[capability])

        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            r = method(*args, **kwargs)
            if inspect.isawaitable(r):
                return asyncio.run(r)
            return r

    return _FakeManager()


class _Provider:
    def __init__(self, capability_id):
        self.capability_id = capability_id

    @property
    def manifest(self):
        class _M:
            id = "fnack.test-media"
            name = "TestMedia"
        return _M()

    def trigger_scan(self):
        return False, "no scan"

    def health(self):
        return None

    def test_connection(self):
        return False, "no connection"


def test_service_has_no_provider_imports_or_id_branches() -> None:
    """Source-level: MediaServerService imports only the SDK; never names a
    provider or branches on provider IDs."""
    src = (ROOT / "services" / "media_server_service.py").read_text(encoding="utf-8")
    for needle in ["services.navidrome_service", '"fnack.navidrome"',
                   "provider.manifest.id ==", "navidrome"]:
        assert needle not in src, f"MediaServerService must stay provider-neutral ({needle})"
    for cap in ["media.scan", "media.health", "media.connection_test"]:
        assert cap in src, f"MediaServerService must resolve {cap}"


def test_zero_providers_raise_capability_unavailable() -> None:
    """Zero enabled providers -> structured CapabilityUnavailable for each
    method (no hidden fallback)."""
    from services.media_server_service import CapabilityUnavailable, MediaServerService

    svc = MediaServerService(manager=_fake_manager())
    cases = [
        (lambda: svc.scan(), "media.scan"),
        (lambda: svc.health(), "media.health"),
        (lambda: svc.test_connection(), "media.connection_test"),
    ]
    for call, cap in cases:
        try:
            call()
        except CapabilityUnavailable as e:
            assert e.capability == cap, f"expected {cap}, got {e.capability}"
        else:
            raise AssertionError(f"expected CapabilityUnavailable for {cap}")


def test_scan_first_success_wins() -> None:
    from services.media_server_service import MediaServerService

    fail = _Provider("media.scan")
    ok = _Provider("media.scan")
    ok.trigger_scan = lambda: (True, "scan started")
    svc = MediaServerService(manager=_fake_manager(fail, ok))
    assert svc.scan() == (True, "scan started")


def test_health_first_non_empty_wins() -> None:
    from services.media_server_service import MediaServerService

    empty = _Provider("media.health")
    ok = _Provider("media.health")
    ok.health = lambda: {"ok": True, "version": "0.54"}
    svc = MediaServerService(manager=_fake_manager(empty, ok))
    assert svc.health() == {"ok": True, "version": "0.54"}


def test_test_connection_candidate_config_forwarded() -> None:
    """test_connection(candidate_config) forwards UNSAVED settings to
    providers that accept them (brief §Candidate configuration); providers
    that don't are called with their stored config."""
    from services.media_server_service import MediaServerService

    captured = {}

    class AcceptsCandidate:
        capability_id = "media.connection_test"
        @property
        def manifest(self):
            class _M:
                id = "fnack.accepts-candidate"
                name = "AcceptsCandidate"
            return _M()
        def test_connection(self, candidate_config=None):
            captured["candidate"] = candidate_config
            return True, "ok with candidate"

    class StoredOnly:
        capability_id = "media.connection_test"
        @property
        def manifest(self):
            class _M:
                id = "fnack.stored-only"
                name = "StoredOnly"
            return _M()
        def test_connection(self):
            captured["stored"] = True
            return False, "stored only"

    # AcceptsCandidate first -> candidate forwarded
    svc = MediaServerService(manager=_fake_manager(AcceptsCandidate(), StoredOnly()))
    ok, msg = svc.test_connection({"url": "http://x", "user": "u", "token": "t"})
    assert ok is True
    assert captured.get("candidate") == {"url": "http://x", "user": "u", "token": "t"}

    # StoredOnly first -> called without candidate, still works
    captured.clear()
    svc2 = MediaServerService(manager=_fake_manager(StoredOnly(), AcceptsCandidate()))
    ok2, msg2 = svc2.test_connection({"url": "http://x"})
    assert ok2 is True
    assert captured.get("stored") is True


def test_callers_migrated_to_application_service() -> None:
    """app.py / queue_service no longer import navidrome_service for scan or
    test (app.py keeps run_auto_split_repair — the split-repair library task,
    which has no capability yet)."""
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "MediaServerService" in app_src
    assert "test_navidrome_connection" not in app_src
    assert "trigger_navidrome_scan" not in app_src
    # run_auto_split_repair (the split-repair library task) has no capability
    # yet — its function-level import stays transitional.
    assert "from services.navidrome_service import run_auto_split_repair" in app_src

    queue_src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    assert "MediaServerService" in queue_src
    assert "trigger_navidrome_scan" not in queue_src
    assert "navidrome_service" not in queue_src


if __name__ == "__main__":
    test_service_has_no_provider_imports_or_id_branches()
    test_zero_providers_raise_capability_unavailable()
    test_scan_first_success_wins()
    test_health_first_non_empty_wins()
    test_test_connection_candidate_config_forwarded()
    test_callers_migrated_to_application_service()
    print("test_media_server_service: PASSED")
