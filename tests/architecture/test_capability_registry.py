"""Architecture test: CapabilityRegistry (Phase 1, MASTER §Architecture tests).

Verifies: register/lookup/ordering/availability, multiple providers per
capability, zero providers, priority ordering (priorities are core), and
unregister on disable semantics.

Run from the repo root:

    .venv/bin/python tests/architecture/test_capability_registry.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fnack.plugin_api.capabilities import CapabilityRegistry
from fnack.plugin_api import (
    DOWNLOAD_TRACK,
    FINGERPRINT_IDENTIFY,
    MEDIA_SCAN,
    TRACK_RESOLVE,
)


class FakeSpotiflac:
    priority = 10


class FakeYtdlp:
    priority = 50


class FakeResolver:
    priority = 30


class FakeAcoustid:
    priority = 100


def test_register_and_lookup() -> None:
    reg = CapabilityRegistry()
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK])
    reg.register("fnack.ytdlp", FakeYtdlp(), [DOWNLOAD_TRACK])

    handles = reg.providers(DOWNLOAD_TRACK)
    assert [h.plugin_id for h in handles] == ["fnack.spotiflac", "fnack.ytdlp"]
    assert reg.has(DOWNLOAD_TRACK) is True
    assert reg.has(FINGERPRINT_IDENTIFY) is False


def test_priority_ordering_is_core() -> None:
    """Priorities remain a core part of the architecture: providers() returns
    LOWEST priority number FIRST (matching fnack's downloader/metadata chain
    semantics: spotiflac p10 before ytdlp p50)."""
    reg = CapabilityRegistry()
    reg.register("fnack.ytdlp", FakeYtdlp(), [DOWNLOAD_TRACK], priority=50)
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK], priority=10)
    reg.register("fnack.resolver", FakeResolver(), [TRACK_RESOLVE])

    assert [h.plugin_id for h in reg.providers(DOWNLOAD_TRACK)] == [
        "fnack.spotiflac", "fnack.ytdlp",
    ]
    # Explicit priority overrides the provider's class attribute.
    reg.register("fnack.ytdlp", FakeYtdlp(), [DOWNLOAD_TRACK], priority=5)
    assert [h.plugin_id for h in reg.providers(DOWNLOAD_TRACK)] == [
        "fnack.ytdlp", "fnack.spotiflac",
    ]


def test_capability_specific_priority() -> None:
    """Phase 1.1: priority is per (plugin_id, capability_id). One plugin can
    serve different capabilities at different priorities."""
    reg = CapabilityRegistry()
    # fnack.spotify: track.resolve priority 5, track.metadata priority 30.
    reg.register(
        "fnack.spotify", FakeResolver(), [TRACK_RESOLVE, "track.metadata"],
        priority=20,                       # plugin-level default
        priorities={"track.resolve": 5, "track.metadata": 30},
    )
    # Another track.metadata provider at priority 25.
    reg.register("fnack.other", object(), ["track.metadata"], priority=25)

    assert [h.plugin_id for h in reg.providers(TRACK_RESOLVE)] == ["fnack.spotify"]
    assert reg.priority_for("fnack.spotify", TRACK_RESOLVE) == 5
    # track.metadata orders by the per-capability priorities, not the
    # plugin-level default (20 would put fnack.spotify first otherwise).
    assert [h.plugin_id for h in reg.providers("track.metadata")] == [
        "fnack.other", "fnack.spotify",
    ]
    assert reg.priority_for("fnack.spotify", "track.metadata") == 30
    # Capability-specific priority overrides plugin-level default.
    assert reg.priority_for("fnack.other", "track.metadata") == 25


def test_providers_for_returns_capability_records() -> None:
    """Phase 1.1: providers_for(capability) returns provider/plugin_id/
    capability_id/effective_priority in deterministic order."""
    reg = CapabilityRegistry()
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK], priority=10)
    reg.register("fnack.ytdlp", FakeYtdlp(), [DOWNLOAD_TRACK], priority=50)
    records = reg.providers_for(DOWNLOAD_TRACK)
    assert [(r.plugin_id, r.capability_id, r.priority) for r in records] == [
        ("fnack.spotiflac", DOWNLOAD_TRACK, 10),
        ("fnack.ytdlp", DOWNLOAD_TRACK, 50),
    ]
    assert records[0].provider is not None
    # Capability record carries the per-capability effective priority.
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK],
                 priority=10, priorities={DOWNLOAD_TRACK: 3})
    records = reg.providers_for(DOWNLOAD_TRACK)
    assert records[0].priority == 3


def test_ties_broken_deterministically() -> None:
    """Phase 1.1: ties resolve by plugin_id — never installation or dict
    insertion order."""
    reg = CapabilityRegistry()
    reg.register("fnack.zzz", object(), [FINGERPRINT_IDENTIFY], priority=100)
    reg.register("fnack.aaa", object(), [FINGERPRINT_IDENTIFY], priority=100)
    assert [h.plugin_id for h in reg.providers(FINGERPRINT_IDENTIFY)] == [
        "fnack.aaa", "fnack.zzz",
    ]
    assert [p.plugin_id for p in reg.providers_for(FINGERPRINT_IDENTIFY)] == [
        "fnack.aaa", "fnack.zzz",
    ]


def test_multiple_capabilities_per_plugin() -> None:
    """One plugin instance can register many capabilities (MASTER rule 5)."""
    reg = CapabilityRegistry()
    navidrome = object()
    reg.register("fnack.navidrome", navidrome, [MEDIA_SCAN, "media.health", "media.connection_test"])
    assert reg.has(MEDIA_SCAN)
    assert reg.has("media.health")
    assert reg.has("media.connection_test")
    handles = reg.providers(MEDIA_SCAN)
    assert len(handles) == 1
    assert handles[0].plugin_id == "fnack.navidrome"
    assert handles[0].provider is navidrome
    assert handles[0].capabilities == frozenset({MEDIA_SCAN, "media.health", "media.connection_test"})


def test_multiple_providers_per_capability() -> None:
    reg = CapabilityRegistry()
    reg.register("fnack.a", object(), [FINGERPRINT_IDENTIFY])
    reg.register("fnack.b", object(), [FINGERPRINT_IDENTIFY])
    assert len(reg.providers(FINGERPRINT_IDENTIFY)) == 2


def test_zero_providers() -> None:
    reg = CapabilityRegistry()
    assert reg.has(DOWNLOAD_TRACK) is False
    assert reg.providers(DOWNLOAD_TRACK) == []


def test_unregister_plugin() -> None:
    reg = CapabilityRegistry()
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK])
    reg.register("fnack.ytdlp", FakeYtdlp(), [DOWNLOAD_TRACK])
    reg.unregister_plugin("fnack.spotiflac")
    assert [h.plugin_id for h in reg.providers(DOWNLOAD_TRACK)] == ["fnack.ytdlp"]
    assert reg.capabilities_for("fnack.spotiflac") == []
    reg.unregister_plugin("does-not-exist")  # no-op, must not raise


def test_register_replaces_handle() -> None:
    """Re-registering the same plugin replaces its handle (used when priority
    override changes or capabilities update)."""
    reg = CapabilityRegistry()
    reg.register("fnack.spotiflac", FakeSpotiflac(), [DOWNLOAD_TRACK])
    reg.register("fnack.spotiflac", FakeSpotiflac(), [TRACK_RESOLVE])  # changed capabilities
    assert reg.has(DOWNLOAD_TRACK) is False
    assert reg.has(TRACK_RESOLVE) is True
    assert reg.capabilities_for("fnack.spotiflac") == [TRACK_RESOLVE]


if __name__ == "__main__":
    test_register_and_lookup()
    test_priority_ordering_is_core()
    test_capability_specific_priority()
    test_providers_for_returns_capability_records()
    test_ties_broken_deterministically()
    test_multiple_capabilities_per_plugin()
    test_multiple_providers_per_capability()
    test_zero_providers()
    test_unregister_plugin()
    test_register_replaces_handle()
    print("test_capability_registry: PASSED")
