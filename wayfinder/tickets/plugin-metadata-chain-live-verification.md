# Verify: metadata-chain fix from PR #6 is live on main

wayfinder:task

## Question

HARNESS BRIEF 5 §1: an external read-only audit reported that PR #6's
metadata-chain fix (per-provider keying, `served_by` logging, per-provider
try/except) did not appear in a direct fetch of `app.py` on main — either a
stale/cached GitHub blob on the reading side, a fetch racing a concurrent
push, or a genuine revert. Confirm which version is actually on main using
repo/shell access.

## Resolution

CONFIRMED: **PR #6's fix is intact on main.** Direct inspection of the
current main tree (`6f4d043`):

- `app.py` `_sync_artist_discography_background`: `served_by = None` (L472),
  per-provider keying `key = str(deezer_artist_id) if
  provider.manifest.id == "fnack.deezer-batch" else artist.name` (L479),
  per-provider try/except with `logger.debug("[METADATA] provider %s
  discography failed, trying next", ...)` (L483), `served_by` + log (L488-489),
  `served_by = "core:deezer_service"` fallback (L504).
- `services/import_service.py`: same pattern (keying L271, per-provider
  try/except L275).
- `git log -p e323562 -- app.py` (PR #6's merge) shows 4 `served_by`
  references introduced by that merge; the code is unchanged since.

The audit's "old version" reading was a stale/cached GitHub blob page on
the reading side — nothing in the repo contradicts the merged diff. No code
change needed. This ticket exists so the tracker doesn't carry a wrong
"reverted" assumption forward.

Associated phase ticket: `plugin-phase-5-settings-and-descriptions.md`.
