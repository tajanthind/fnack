# Design: Phase 3 — open the marketplace (repositories + install/update/uninstall UI)

wayfinder:research

## Resolution

RESOLVED (research) — findings written to `wayfinder/research/phase-3-marketplace-design.md`. Decisions: tabbed /plugins page (Installed / Marketplace / Repositories); marketplace grid with compat + bundled markers; install/update/uninstall with a Community trust-permission modal (manifest trust wins); light health panel (last_run_at/last_error/auto-disabled badge — latency deferred); generic settings_schema form modal (custom settings_tab slots stay); config-as-code + fnack-cli deferred to Phase 4; bundled plugins never installable/uninstallable (badge + disable instead).

## Question

PLUGIN_ARCHITECTURE.md §5 (repositories & marketplace) + §11 (trust tiers,
permission dialog, health dashboard, config-as-code) + PHASE1 §4.4
("Marketplace / Repositories tabs ... needed for Phase 3, but the
enable/disable and priority UI above should work identically for bundled and
third-party plugins from day one"). The backend scaffold ALREADY exists from
Phase 0: `plugins/registry.py` (add/refresh/remove repository, fetch+cache
index, list_available, install/update/uninstall with sha256 verification) and
`plugins/api.py` routes (`/api/plugins/marketplace`, `/install`,
`/repositories`, `/repositories/<id>/refresh`, `/repositories/<id>` DELETE).
Design decisions (research — no code changes yet):

1. **Settings → Plugins page structure**: the Phase 1 page has one grouped
   list. Phase 3 adds two more tabs/sections: **Repositories** (add/refresh/
   remove repo URLs, show last-synced) and **Marketplace** (browse merged
   index grid: name, description, version, compatibility badge, permissions,
   trust badge; Install / Update buttons). Decide tabs vs. sections on the
   existing `/plugins` page (settings.html-style card sections fit the current
   layout; a tabbed nav is cleaner once there are 3 groups — pick based on
   static/app.js conventions).
2. **Install flow UX**: click Install → download zip → sha256 verify →
   extract to `/config/plugins/<id>/` → `registry.install()` creates the row
   (already implemented) → **community trust confirmation dialog** first
   (permission list + "install anyway" — PLUGIN_ARCHITECTURE.md §6/§11).
   Update → same flow for newer version keeping PluginSetting rows (registry
   already keys settings by plugin_id). Uninstall → confirm dialog →
   `registry.uninstall()` (calls on_disable → on_unload, deletes dir + row).
3. **Trust tiers + permission prompt**: Official/Verified/Community badges
   (already in the Phase 1 list UI); Community installs show the declared
   permissions with an explicit confirm. Where the dialog lives in
   `templates/plugins.html` + `static/app.js`.
4. **Health dashboard**: per-plugin last-run status, error count, average
   latency (PLUGIN_ARCHITECTURE.md §11). The `/api/plugins/<id>/health`
   endpoint exists; the Phase 1 page already shows failure badges. Phase 3
   adds a fuller panel (or extends the row). Decide scope: enough for
   auto-disable visibility + last error, not a full observability suite.
5. **Per-plugin settings panels**: `settings_schema` auto-generates a form
   (type/select/boolean/secret) via a generic renderer in plugins.html + a
   `savePluginSettings`-style POST (pattern already exists from the
   navidrome settings_tab). Decide: generic schema-rendered form for every
   plugin with a non-empty `settings_schema`, replacing hand-rolled tabs.
6. **Config-as-code** (§11, optional): export/import plugin state (repos +
   versions + settings, secrets redacted) as one JSON blob — pairs with
   DEPLOY.md. Decide if it's in Phase 3 scope or a Phase 4 nicety.
7. **fnack-cli plugin install/list/update/remove** (§11, optional): headless
   management for Docker entrypoint use. Decide in-scope or later.
8. **Behavior-preservation**: bundled plugins must NOT appear in the
   marketplace as installable duplicates (they're already installed); the
   marketplace should mark them "Installed (bundled)". Uninstalling a bundled
   plugin should be blocked or warn (it returns on next boot via auto-install
   unless the user disables it instead).

Deliver: `wayfinder/research/phase-3-marketplace-design.md` with the page
structure decision, install/update/uninstall UX flows, trust-dialog spec,
health panel scope, generic settings renderer, config-as-code + CLI
in/out-of-scope calls, and the bundled-plugin edge-case rules. No code
changes.
