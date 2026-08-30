# Phase 3 marketplace — design findings

Status: research (no code changed). Companion ticket:
`wayfinder/tickets/plugin-phase-3-design.md`. Implementation happens on
`plugin-architecture/phase-3-marketplace` after PR #2 merges.

Scope: turn the existing registry/API scaffold (Phase 0) into the user-facing
Settings → Plugins Repositories + Marketplace, with trust dialogs, a health
panel, and per-plugin settings. Bundled plugins must not become installable
duplicates.

---

## 1. Page structure: three tabbed sections on `/plugins`

Decision: **tabs** (nav-tabs), matching the three distinct functions, on the
existing top-level `/plugins` page (`templates/plugins.html`). Bootstrap 5
tabs are already available (used in artist.html filters). Sections:

| Tab | Content |
|---|---|
| **Installed** (default) | The Phase 1 grouped list (unchanged; enable/disable, priority, health badges) |
| **Marketplace** | Merged repo index grid (browse, install, update) |
| **Repositories** | Add/refresh/remove repo URLs + last-synced timestamps |

`static/app.js`: one `loadPluginsPage()` entry that fetches
`/api/plugins/grouped` + `/api/plugins/marketplace` + `/api/plugins/repositories`
in parallel and renders the active tab; tab click = re-render from cached
fetches (no extra network churn).

## 2. Marketplace grid

Per repo entry from `GET /api/plugins/marketplace` (already merges indices,
annotates `installed_version` + `source_repo_id/name`):

```
Card: [name] [trust badge] [compat badge]
      description
      latest_version  |  [Install] or [Update vX→Y] or [Installed (bundled)]
      permissions: network, settings, filesystem:downloads
```

- **Installed state** → "Installed v1.2.0" + (if newer available) "Update"
  button.
- **Bundled** (id matches an `InstalledPlugin` row with `source_repo_id IS
  NULL` AND the id is in `manager.discover_bundled()` ids) → badge
  "Installed (bundled)" and **no install/update button** — bundled wins
  (edge case rule §8).
- **Compat badge**: green if `min_core_version <= core` and `api_version`
  range contains `PLUGIN_API_VERSION`, else red "needs newer fnack".

## 3. Install / Update / Uninstall flows

**Install** (Community trust):
1. Click Install on a community-trust entry → **permission dialog** modal:
   plugin name/version/author, declared permissions list, "This plugin runs
   in-process; only install from sources you trust" warning, [Cancel] /
   [Install anyway].
2. Confirm → `POST /api/plugins/install {plugin_id, version}` → registry
   downloads, sha256-verifies, extracts, `load_plugin` validates manifest,
   creates row (enabled=True), `enable_plugin`.
3. Success toast + switch to Installed tab showing the new plugin.

**Update**: `POST /api/plugins/install {plugin_id}` (version=None →
registry picks latest) — PluginSetting rows survive (keyed by plugin_id).
Same trust dialog if the new version's permissions grew (compare
`settings_schema`/`permissions` — simplest: always re-show the dialog on
update when trust != official).

**Uninstall**: Installed tab row action (only for non-bundled) → confirm
modal → `POST /api/plugins/<id>/uninstall` → `registry.uninstall()`
(on_disable → on_unload → delete dir + row). Bundled: no uninstall button
(disable instead).

## 4. Trust tiers + permission prompt

- Badges already in Phase 1 list UI (Official/Verified/Community).
- Community install confirmation = the modal in §3.1. Verified → shorter
  confirm (name + permissions, no scary copy). Official → no prompt
  (bundled; marketplace never installs official from a repo).
- `trust_level` comes from the manifest (`PluginManifest.trust_level`,
  default community) — repos can't lie: a repo entry's `trust_level` field
  is ignored; the manifest inside the zip wins (registry already stores
  `manifest.trust_level`).

## 5. Health panel scope

Keep it light (Phase 1 page already shows failure badges):
- Extend the Installed row's health area: `last_run_at`, `last_error`
  (truncated), `consecutive_failures`, and a small "auto-disabled" badge
  when `enabled` is false but `consecutive_failures >= 5`.
- No latency histograms / charts in Phase 3 — note as a later nicety
  (PLUGIN_ARCHITECTURE §11 "average latency" deferred).

## 6. Per-plugin settings panels (generic schema renderer)

- Any plugin with non-empty `settings_schema` gets a **Settings** button in
  its Installed row → modal with the auto-generated form:
  - `string` → text input · `number` → number input · `boolean` → checkbox
  - `select` → dropdown from `options` · `secret` → password input
  - `required` → HTML `required` attr
- Save → `POST /api/plugins/<id>/settings` (existing endpoint) →
  `on_settings_changed` fires.
- Replaces hand-rolled per-plugin forms (navidrome's settings_tab slot stays
  for plugins that want a fully custom panel; the generic form is the
  default for `settings_schema`-declared plugins).

## 7. Config-as-code + fnack-cli (scope calls)

- **Config-as-code export/import** (§11): **defer to Phase 4** — it's a
  nice-to-have for DEPLOY.md parity, not required for the marketplace to be
  usable. Design note: export = `{repos: [...urls], plugins:
  {id: {version, enabled, settings}}}` with secrets redacted; import
  re-adds repos + installs pinned versions. (Not built in Phase 3.)
- **`fnack-cli plugin ...`** (§11): **defer to Phase 4** — the Docker
  entrypoint can `curl` the REST API today; a CLI is ergonomics, not
  capability. (Not built in Phase 3.)

## 8. Bundled-plugin edge-case rules

1. Bundled ids (present in `/app/bundled_plugins`) never show Install/Update
   in the marketplace → badge "Installed (bundled)".
2. Bundled rows have no Uninstall button → disable instead (and note the
   disable persists; the row stays).
3. If a repo publishes the same id as a bundled plugin, the marketplace
   shows the bundled entry and hides the repo version (registry
   `list_available` already marks `installed_version`; add an explicit
   "bundled" flag from `discover_bundled()` ids).
4. Uninstall of a non-bundled plugin that a repo later re-lists → normal
   flow; nothing special.

## 9. Summary (implementation order)

1. Tabbed `/plugins` page (Installed / Marketplace / Repositories).
2. Repositories tab: list + add (URL input) + refresh + remove, showing
   last-synced_at.
3. Marketplace grid + compat badge + bundled marker.
4. Install/Update/Uninstall with the trust-permission modal.
5. Generic settings-schema form modal.
6. Health row extension (last_run_at / last_error / auto-disabled badge).
7. Behavior-preservation guard: bundled plugins never installable/uninstallable.
