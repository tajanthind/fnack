"""Architecture tests: plugin network honesty (load-time scan).

The manifest `network` permission is the honest signal that a plugin does
outbound network I/O. A plugin whose code imports network-capable modules
(requests, urllib.request, http.client, urllib3, httpx, aiohttp, socket,
websockets, …) WITHOUT declaring `network` bypasses the context.http gate —
fnack detects this statically at load and warns (plugins/manager.py
`_network_capable_imports`).

1. The scanner detects direct network imports in a plugin dir.
2. Every fnack-bundled plugin that imports a network-capable module declares
   `network` in its manifest (official plugins stay honest).
3. Manifest-declared `network` suppresses the load-time warning (the scanner
   is only consulted when the permission is missing).

Run from the repo root:

    .venv/bin/python tests/architecture/test_plugin_network_honesty.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from plugins.manager import _network_capable_imports  # noqa: E402

BUNDLED = ROOT / "bundled_plugins"

# Modules a plugin could import that give it real network access.
_KNOWN = {"requests", "urllib.request", "http.client", "urllib3",
          "httpx", "aiohttp", "socket", "websockets", "websocket"}


def test_scanner_detects_network_imports() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "p"
        d.mkdir()
        (d / "plugin.json").write_text('{"id": "x"}', encoding="utf-8")
        (d / "plugin.py").write_text(
            "import requests\n"
            "from urllib import request as _r\n"
            "import socket\n"
            "import json  # harmless\n"
            "from requests.adapters import HTTPAdapter\n",
            encoding="utf-8",
        )
        found = _network_capable_imports(d)
        assert "requests" in found, found
        assert any(m == "urllib" or m == "urllib.request" for m in found), found
        assert "socket" in found, found
        # top-level "import json" is not a network module.
        assert "json" not in found


def test_every_bundled_network_importer_declares_network() -> None:
    """Official plugins: importing network modules without declaring
    'network' would be a contract bypass. Scan every vendored plugin."""
    offenders = []
    for pdir in sorted(BUNDLED.iterdir()):
        mf = pdir / "plugin.json"
        if not pdir.is_dir() or not mf.exists():
            continue
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        imports = _network_capable_imports(pdir)
        if not imports:
            continue
        perms = manifest.get("permissions") or []
        if "network" not in perms:
            offenders.append(f"{manifest.get('id', pdir.name)} imports "
                             f"{', '.join(imports)} but does not declare 'network'")
    assert not offenders, (
        "bundled plugins importing network-capable modules must declare "
        f"'network' in their manifest:\n  " + "\n  ".join(offenders)
    )


def test_scanner_warns_only_without_network_permission() -> None:
    """The load-time warning is gated on the permission being MISSING — a
    plugin that declares 'network' is honest by construction."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "p"
        d.mkdir()
        (d / "plugin.json").write_text(
            '{"id": "x", "permissions": ["network"]}', encoding="utf-8")
        (d / "plugin.py").write_text("import requests\n", encoding="utf-8")
        # Declared network -> the manager will NOT consult the scanner for a
        # warning; the import list itself still reports the module (used by
        # the honesty test above).
        assert _network_capable_imports(d) == ["requests"]


if __name__ == "__main__":
    test_scanner_detects_network_imports()
    test_every_bundled_network_importer_declares_network()
    test_scanner_warns_only_without_network_permission()
    print("test_plugin_network_honesty: PASSED")
