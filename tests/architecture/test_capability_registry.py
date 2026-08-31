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
    test_multiple_capabilities_per_plugin()
    test_multiple_providers_per_capability()
    test_zero_providers()
    test_unregister_plugin()
    test_register_replaces_handle()
    print("test_capability_registry: PASSED")
