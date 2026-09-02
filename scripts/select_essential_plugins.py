#!/usr/bin/env python3
"""Docker build step: prune ``bundled_plugins/`` to the ESSENTIAL set.

The fnack image must contain only core + the essential plugins (see
``plugins/essential.py`` — the single source of truth for the set). This
script deletes every plugin directory in the target ``bundled_plugins`` dir
that is not in ``ESSENTIAL_PLUGINS``, so startup auto-installs exactly the
essential set and nothing else ships in the image.

Optional official plugins stay installable through the Marketplace
(fnack-plugins repository) — pruning them from the image does not remove any
capability, it just stops baking them in.

Usage (as in the Dockerfile):

    python3 scripts/select_essential_plugins.py [BUNDLED_PLUGINS_DIR]

The argument defaults to ``<repo>/bundled_plugins``; it is also safe to run
against a copy (e.g. /app/bundled_plugins inside the image) — the script only
touches directories under the given path that contain a ``plugin.json`` whose
``id`` is not essential, and never removes the dir itself.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.essential import ESSENTIAL_PLUGINS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    target = Path(argv[0]) if argv else ROOT / "bundled_plugins"
    target = target.resolve()

    if not target.is_dir():
        print(f"select_essential_plugins: no bundled_plugins dir at {target}", file=sys.stderr)
        return 1

    removed: list[str] = []
    kept: list[str] = []
    for pdir in sorted(target.iterdir()):
        if not pdir.is_dir():
            continue
        manifest_path = pdir / "plugin.json"
        if not manifest_path.exists():
            continue  # not a plugin dir; leave it alone
        try:
            pid = json.loads(manifest_path.read_text(encoding="utf-8")).get("id")
        except Exception:
            pid = None
        if pid in ESSENTIAL_PLUGINS:
            kept.append(pid)
        else:
            shutil.rmtree(pdir)
            removed.append(pid or pdir.name)

    kept.sort()
    removed.sort()
    print(f"select_essential_plugins: {target}")
    print(f"  kept ({len(kept)}): {', '.join(kept)}")
    print(f"  removed ({len(removed)}): {', '.join(removed)}")
    if removed:
        print("  (removed plugins remain installable from the fnack-plugins Marketplace)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
