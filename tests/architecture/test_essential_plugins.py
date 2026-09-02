"""Architecture test: essential-plugin packaging policy (final Phase 4 cleanup).

The Docker image ships ONLY the ESSENTIAL plugin set; `plugins/essential.py`
(`ESSENTIAL_PLUGINS`) is the single source of truth. This test pins the
consistency between that constant and the repo:

- every essential plugin id has a vendored dir in `bundled_plugins/` with a
  manifest and entry module (so the image build never prunes a missing dir);
- every non-essential bundled plugin is documented as *optional* and must NOT
  be part of the essential set (they stay installable from the Marketplace);
- the Dockerfile bakes the image from the pruned set (no bare re-copy of the
  full `bundled_plugins/` after pruning);
- the prune script (`scripts/select_essential_plugins.py`) actually works:
  running it against a COPY of `bundled_plugins/` leaves exactly the essential
  set.

The repo's `bundled_plugins/` deliberately keeps the full official catalog
(vendored from fnack-plugins) so extraction-parity tests and dev runs see
every official plugin; only the *image* is pruned.

Run from the repo root:

    .venv/bin/python tests/architecture/test_essential_plugins.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from plugins.essential import ESSENTIAL_PLUGINS  # noqa: E402

BUNDLED = ROOT / "bundled_plugins"
DOCKERFILE = ROOT / "Dockerfile"
SELECT_SCRIPT = ROOT / "scripts" / "select_essential_plugins.py"


def _bundled_ids() -> dict[str, Path]:
    """{plugin id: dir} for every vendored plugin with a manifest."""
    out = {}
    for pdir in BUNDLED.iterdir():
        manifest = pdir / "plugin.json"
        if not pdir.is_dir() or not manifest.exists():
            continue
        try:
            pid = json.loads(manifest.read_text(encoding="utf-8")).get("id")
        except Exception:
            pid = None
        if pid:
            out[pid] = pdir
    return out


def test_every_essential_plugin_is_vendored() -> None:
    """Each essential plugin must have a bundled dir with manifest + entry."""
    bundled = _bundled_ids()
    missing = ESSENTIAL_PLUGINS - set(bundled)
    assert not missing, (
        f"ESSENTIAL_PLUGINS references plugins with no bundled dir: "
        f"{sorted(missing)} — add them to bundled_plugins/ or drop them from "
        "plugins/essential.py"
    )
    for pid in ESSENTIAL_PLUGINS:
        pdir = bundled[pid]
        assert (pdir / "plugin.json").exists() and (pdir / "plugin.py").exists(), \
            f"essential plugin {pid} must ship manifest + entry module"


def test_optional_plugins_are_not_essential() -> None:
    """Every other bundled plugin is optional (not in the essential set) and
    remains fully installable from the Marketplace."""
    bundled = set(_bundled_ids())
    optional = bundled - ESSENTIAL_PLUGINS
    assert optional, "expected optional official plugins in bundled_plugins/"
    # Sanity: the optional set includes representative non-essential features.
    for pid in ["fnack.musicbrainz", "fnack.acoustid", "fnack.navidrome",
                "fnack.vpn", "fnack.subsonic"]:
        assert pid in optional, f"{pid} should be optional (not essential)"
    # The deep architecture doc explicitly calls the optional ones installable.
    doc = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "remains installable from the fnack-plugins" in doc


def test_dockerfile_prunes_to_essential() -> None:
    """The Dockerfile must prune bundled_plugins via the selection script and
    must not re-copy the full catalog after pruning."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "select_essential_plugins.py" in dockerfile, \
        "Dockerfile must run scripts/select_essential_plugins.py to bake only essential plugins"
    prune_index = dockerfile.find("select_essential_plugins.py")
    assert prune_index != -1
    tail = dockerfile[prune_index:]
    assert "COPY bundled_plugins /app/bundled_plugins" not in tail, \
        "Dockerfile must not re-copy the full bundled_plugins after pruning"


def test_selection_script_prunes_a_copy_to_exactly_essential() -> None:
    """Functional check of the prune step on a throwaway copy."""
    assert SELECT_SCRIPT.exists()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "bundled_plugins"
        shutil.copytree(BUNDLED, target)
        proc = subprocess.run(
            [sys.executable, str(SELECT_SCRIPT), str(target)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"selection script failed: {proc.stderr}"
        remaining = {
            json.loads((p / "plugin.json").read_text(encoding="utf-8"))["id"]
            for p in target.iterdir() if (p / "plugin.json").exists()
        }
        assert remaining == set(ESSENTIAL_PLUGINS), (
            f"selection script left {sorted(remaining - set(ESSENTIAL_PLUGINS))} "
            f"and dropped {sorted(set(ESSENTIAL_PLUGINS) - remaining)}"
        )


def test_essential_set_covers_first_run_workflow() -> None:
    """The essential set must cover the first-run workflow: add artist
    (artist.search), sync discography (artist.discography), resolve Spotify
    links (track.resolve), download (download.track, primary + fallback)."""
    from plugins.essential import ESSENTIAL_PLUGINS as E
    capabilities = {}
    for pid in E:
        manifest = json.loads((BUNDLED / pid / "plugin.json").read_text(encoding="utf-8"))
        capabilities[pid] = set(manifest.get("capabilities", []))
    assert "artist.search" in capabilities["fnack.deezer-batch"]
    assert "artist.discography" in capabilities["fnack.deezer-batch"]
    assert "track.resolve" in capabilities["fnack.spotify"]
    assert "download.track" in capabilities["fnack.spotiflac"]
    assert "download.track" in capabilities["fnack.ytdlp"]
    # No essential plugin may be a "library.task" consumer — maintenance is a
    # core subprocess, so those stay optional.
    for pid in E:
        types = json.loads((BUNDLED / pid / "plugin.json").read_text(encoding="utf-8")).get("type", [])
        if isinstance(types, str):
            types = [types]
        assert "library_task" not in types, f"{pid} is a library-task plugin and must be optional"


if __name__ == "__main__":
    test_every_essential_plugin_is_vendored()
    test_optional_plugins_are_not_essential()
    test_dockerfile_prunes_to_essential()
    test_selection_script_prunes_a_copy_to_exactly_essential()
    test_essential_set_covers_first_run_workflow()
    print("test_essential_plugins: PASSED")
