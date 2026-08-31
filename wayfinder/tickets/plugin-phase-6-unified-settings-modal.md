# Phase 6: unified plugin-settings modal, in-place updates, version-mismatch state, descriptions

wayfinder:grilling

## Question

HARNESS BRIEF 6, branch `plugin-architecture/phase-6-unified-settings-modal`:
(1) identify the actual render paths for VPN/Navidrome/Subsonic settings,
(2) unify every plugin's settings into the single schema modal (file type +
actions + status), (3) in-place plugin updates with settings preserved and
a bundled-vs-marketplace decision, (4) visible "Unsupported" state for
version-incompatible plugins, (5) confirm descriptions render everywhere.

## Resolution

1. **§1 — all three use the SAME render path**: each of
   `fnack.vpn`/`fnack.navidrome`/`fnack.subsonic` had a
   `_render_settings_tab` registered via `on_load`, all rendered by the
   single `{{ plugin_slot('settings_tab') }}` on `/plugins`. There was no
   third bespoke path — the inconsistency was inline-cards (slot) vs
   schema→modal for the other 9 plugins. (The audit's screenshots were
   read from live UI; the code confirms one slot mechanism.)

2. **§2 — unified modal, no exceptions.** Schema system extended:
   - `"file"`/`"secret_file"` schema type (already present from Brief 5
     port) + `POST /api/plugins/<id>/file` per-plugin private upload.
   - **`actions` array** added to `PluginManifest` + validation +
     `POST /api/plugins/<id>/action/<id>` route (calls the snake_cased
     method, tuple[bool,str] aware) + rendered as buttons in the modal.
   - **`GET /api/plugins/<id>/status`** — modal polls it for live status
     (VPN Running/Stopped + public IP).
   - VPN: `config_file` (file) + `start`/`stop` actions; `on_settings_changed`
     copies the uploaded file into VPN_DIR for vpn_service. Navidrome: its 5
     schema fields (url/user/token/auto_scan/db_path) mapped to the
     `navidrome_*` AppSetting rows, slot retired. Subsonic: `enabled` via
     modal, slot retired.
   - `{{ plugin_slot('settings_tab') }}` removed from `/plugins` — **zero
     inline cards**. Live-verified: VPN status/upload/start/stop through
     the modal endpoints; undeclared action refused; all 17 plugins carry
     schema + actions keys.

3. **§3 — in-place updates.** `registry.update()` already existed
   (install-with-latest); added `POST /api/plugins/<id>/update`,
   `update_available` flag on the Installed list (from repo cached index
   `latest_versions()`), and an Update button. **Decision: bundled plugins
   do NOT get Update** — they update with the fnack image
   (`docker compose pull && up -d`); only marketplace-installed plugins
   update independently. Settings (`plugin_settings` keyed by plugin_id)
   survive updates; removed keys are kept in storage, just not shown.

4. **§4 — visible Unsupported state.** Previously an incompatible plugin
   threw PluginLoadError and VANISHED silently. Now `VersionMismatchError`
   (subclass) is raised for api_version/min_core_version failures;
   `PluginManager._load_failures` records the reason; `list_loaded()`
   appends failed plugins with `load_error`. The Installed row shows
   "Unsupported — requires core ≥ X, you're on Y" (or a generic failed-to-
   load badge for other errors) with the Settings icon disabled; the
   Marketplace computes real compat (`min_core_version`/`api_version`
   annotated in `list_available`) and greys out incompatible entries with
   the reason. Live-verified: bumped ntfy min_core_version to 99.0.0 →
   appears with `load_error: "requires fnack >= 99.0.0, running 0.3.1"`,
   restored cleanly.

5. **§5 — descriptions confirmed everywhere.** `list_loaded()` passes
   `description` (added in the Brief 5 port); Installed rows render it
   (truncate + tooltip); Marketplace cards already did (`e.description`).
   All 17 entries carry descriptions.

Tracker: this ticket; map entry appended. Smoke test extended (actions,
load_error on version-mismatch fixture, bundled-update refusal) — passes.
