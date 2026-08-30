# Design: Phase 4 — stretch goals (auth_provider, webhook pack, Subsonic, dep isolation)

wayfinder:research

## Resolution

RESOLVED (research) — findings written to `wayfinder/research/phase-4-stretch-design.md`. Scope calls: webhook pack (Discord + ntfy as two event_hook plugins) IN, config-as-code export/import IN, auth_provider (reverse-proxy header first, zero-auth guard) IN, Subsonic server_extension IN (flagship, raw streaming), per-plugin dep isolation design-ready but deferred until after Phase 3, signed manifests + update channels DEFERRED (post-Phase-4 optional).

## Question

PLUGIN_ARCHITECTURE.md §11 "Extra features worth having" + HARNESS §3 (fold
types now, implement later) + PHASE1 §2.4. Phase 4 is the stretch phase —
each item below is independent and can be shipped in any order / skipped.
Design decisions (research — no code changes yet):

1. **`auth_provider` plugin type** (HARNESS §3, Phase 4 stretch): SSO /
   reverse-proxy-header auth as a plugin for people running fnack behind
   Authelia/Authentik. Constraint: fnack is zero-required-auth — core auth is
   optional (API key for M2M only); an auth_provider plugin must not make auth
   *required* unless the user explicitly installs + enables it. The
   `AuthProviderPlugin` interface already exists in `plugins/base.py`
   (`authenticate(request_headers) -> Optional[str]`). Design: where the
   auth check hooks into Flask request handling (before_request), how a
   plugin-provided identity maps to fnack's current no-auth model, and the
   guard that core stays open when no auth_provider is installed.
2. **Webhook/notification pack** (HARNESS §3: Discord + ntfy as TWO separate
   `event_hook` plugins — trust tiers per service): each subscribes to
   `queue.job_completed` / `queue.job_failed` / `track.caution_flagged` and
   POSTs a webhook. Design: settings_schema (webhook URL, per-event
   toggles), payload shape, rate/spam guard (debounce), and where the events
   are emitted (already in queue_service from Phase 0/1: `track.after_download`,
   `track.verified`; need `queue.job_completed`/`queue.job_failed` +
   `track.caution_flagged` emitted too).
3. **Subsonic-API `server_extension` plugin** (§11 flagship): lets
   Symfonium/DSub/Sublime Music treat fnack itself as a Subsonic server,
   independent of Navidrome. Design: which Subsonic endpoints to implement
   first (ping, getLicense, getArtists, getAlbumList2, stream, getCoverArt),
   auth (Subsonic token auth vs fnack's no-auth — likely the API key),
   stream transcoding (fnack stores flac/opus — stream raw vs transcode),
   and the `ServerExtensionPlugin.register_routes(blueprint)` wiring (already
   in the scaffold).
4. **Per-plugin Python dependency isolation** (§11): install each plugin's
   `dependencies.python` into a private site-packages dir on sys.path for
   that plugin only. Design: how `_import_module` sets up the isolated path,
   pip install target dir, and the failure mode when deps can't be resolved
   (load error, not crash).
5. **Signed manifests** (§11, optional): repos publish a detached signature
   per release; core verifies before install if the repo publishes a public
   key. Design: where the signature lives in the index, verification in
   `registry.install()`, and the "Verified" badge tie-in.
6. **Per-plugin update channel + changelog** (§11, optional): stable/beta
   channels, changelog diff before applying. Design: index version fields.
7. **Config-as-code export/import** (§11, Phase 3 design deferred here):
   export/import plugin state (repos + versions + settings, secrets
   redacted) as one JSON blob.

Deliver: `wayfinder/research/phase-4-stretch-design.md` with per-item scope
call (in/out/deferred), the auth_provider hook design, webhook payload
spec, Subsonic endpoint subset, dep-isolation approach, and the other
items' status. No code changes.
