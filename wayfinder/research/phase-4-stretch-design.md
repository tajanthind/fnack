# Phase 4 stretch goals — design findings

Status: research (no code changed). Companion ticket:
`wayfinder/tickets/plugin-phase-4-design.md`. Scope calls per item —
everything here is independent, shippable in any order, and none is
required for Phases 0–3 to be complete.

---

## 1. `auth_provider` plugin type — IN SCOPE (when Phase 4 starts)

Interface already exists (`plugins/base.py`:
`AuthProviderPlugin.authenticate(request_headers) -> Optional[str]`).

**Zero-required-auth guard (critical):** fnack's core is unauthenticated;
the API key is optional (M2M/Lidarr). An auth_provider must be strictly
opt-in:
- Flask `before_request` hook added by the plugin manager ONLY when at least
  one auth_provider plugin is installed AND enabled. With zero auth_providers
  installed, the hook doesn't exist — core stays fully open (unchanged).
- If an auth_provider is enabled and `authenticate()` returns None →
  the request is rejected with 401 (the plugin decides the response shape);
  if it returns a username, `g.fnack_user = username` and the request
  proceeds.
- Multiple auth_providers: first one returning a username wins; all-None →
  401. (Chain semantics mirror metadata_provider.)
- Core routes needing auth when a provider is active: everything except
  `/health` + static assets. (Design choice: protect all, allowlist
  health/static.)
- The API key (existing M2M path) must still work when an auth_provider is
  active (X-API-Key header short-circuits to the same `g.fnack_user`).

**Reverse-proxy-header provider** (first bundled example): reads e.g.
`X-Authentik-Username` / `X-Forwarded-User` after the user configures the
trusted header name. Manifest settings: `header_name` (default
`X-Forwarded-User`), `trusted_proxy` (optional — warn if not set, since the
header is spoofable without one).

**Do NOT ship an OAuth/SSO flow plugin in the first cut** — reverse-proxy
header is the 80% case for Authelia/Authentik; full OIDC is a much bigger
surface (redirects, state, PKCE) and can be a second plugin later.

## 2. Webhook/notification pack — IN SCOPE (Discord + ntfy, two plugins)

HARNESS §3: two SEPARATE `event_hook` plugins (per-service trust tiers),
not one configurable plugin.

**Events to emit** (add to queue_service if missing):
- `queue.job_completed` / `queue.job_failed` — emit in `_process_track_job`
  at the same points as the existing success/failure logs (after
  `track.after_download`, and in the failure branch). Payload:
  `{job_id, track_id, title, artist_name, album_name, status}`.
- `track.caution_flagged` — emit in the AcoustID caution path (where
  `flagged_caution` is set). Payload: `{track_id, matched_title, matched_artist, score}`.
- `track.after_download` / `track.verified` already exist (Phase 0/1).

**`fnack.discord-webhook` plugin**:
- settings_schema: `webhook_url` (secret), `events` (multi — simplest: four
  boolean toggles `on_job_completed`, `on_job_failed`, `on_caution_flagged`,
  `on_track_verified`), `username` (optional, default "fnack").
- Payload: Discord embed `{title, color (green/red/amber), fields:
  [artist, album, track], footer: fnack <version>}`.
- POST to `context.http` (declares `network`); 2xx/4xx accepted, 5xx logged
  + retried once after 5s; never blocks the queue (fire-and-forget greenlet).

**`fnack.ntfy-webhook` plugin**: same events, ntfy POST
`{topic, title, message, tags}` via `context.http`; settings: `server_url`
(default `https://ntfy.sh`), `topic`, per-event toggles.

**Spam guard**: debounce identical job_failed messages (same track id within
60s) via a small in-memory set per plugin.

## 3. Subsonic-API `server_extension` plugin — IN SCOPE (flagship)

Lets Symfonium/DSub/Sublime Music treat fnack itself as a Subsonic server
(independent of Navidrome).

**First-cut endpoint subset** (all JSON via `?format=json`):
- `ping` / `getLicense` — always ok (license not applicable).
- `getArtists` — fnack artists → Subsonic artist list.
- `getAlbumList2` (type=alphabetical/newest/recent) — fnack albums.
- `getArtist` — artist + albums + (optionally) tracks.
- `getAlbum` — album + tracks.
- `getSong` — track info.
- `stream?id=<track_id>` — serve the audio file (Range requests for
  seeking; content-type from extension).
- `getCoverArt` — album cover.jpg.
- `getScanStatus` / `startScan` — no-op ok (fnack manages its own library).

**Auth**: Subsonic clients send `u/p` or token+salts. Map to fnack's API
key: `p` (or legacy plaintext) compared against the stored `api_key`
AppSetting; `token/salt` = md5(password+salt) — support the token scheme.
If no API key is set, auth is open (matches fnack's zero-auth model).
`<base>/rest/<method>?u=...&t=...&s=...&v=1.16.1&c=client&f=json`.

**Streaming**: serve raw file bytes (flac/opus/mp3 as stored) with proper
Content-Type; clients that need mp3 transcode via their own settings — no
transcoding in the first cut (ffmpeg transcode = later enhancement; note
the container has no ffmpeg today).

**Wiring**: `ServerExtensionPlugin.register_routes(blueprint)` — the plugin
builds its own Blueprint (`/rest/*` + `/rest/stream` etc.), registers it in
`on_load`. The `server_extension` type is already in VALID_TYPES + base.py.

## 4. Per-plugin Python dependency isolation — IN SCOPE (design), defer impl

`PluginManifest.dependencies.python` = list of pip specs. Approach:
- In `PluginManager._import_module`, before `exec_module`, if the manifest
  declares deps: create `<plugin_dir>/deps/`, `pip install --target` the
  specs once (cache by (id, version)), and insert that dir at the FRONT of
  `sys.path` for the duration of the import + plugin lifetime (restore on
  unload). Two plugins never fight over versions — each has its own dir.
- Failure mode: pip failure → `PluginLoadError` (load error in the UI, never
  a crash). Health log shows the reason.
- **Defer implementation**: pip-in-container at plugin-load time adds boot
  cost and needs `pip` present in the image (it is). Design is ready; ship
  after the marketplace (Phase 3) so third-party plugins with deps can
  actually install.

## 5. Signed manifests — DEFER (optional, later)

Repos publish a detached signature per release (e.g. `signature` field in
`versions.<v>` = base64 ed25519 sig over the zip sha256); core verifies in
`registry.install()` if the repo row has `public_key`. Gives "Verified"
badges teeth. No design blocker — just needs a key-management story
(where the user pastes the repo's public key in the Repositories tab).
Mark as post-Phase-4 optional.

## 6. Per-plugin update channel + changelog — DEFER (optional)

Index gains `channel` (stable/beta) + `changelog` per version; Marketplace
shows "Update to vX (changelog)" diff before applying. Requires Phase 3's
Marketplace first. Post-Phase-4 optional.

## 7. Config-as-code export/import — IN SCOPE (Phase 4, low effort)

New API endpoints:
- `GET /api/plugins/export` → `{repos: [{url, enabled}],
  plugins: {id: {version, enabled, settings, priority_override}}}` with
  secret values redacted (`"<redacted>"`).
- `POST /api/plugins/import` → re-adds repos, installs pinned versions,
  restores enabled + settings + priority. Reuses `registry.add_repository` +
  `registry.install(plugin_id, version)`.
- Pairs with DEPLOY.md "move to another machine" story.

---

## 8. Summary

| Item | Scope |
|---|---|
| auth_provider (reverse-proxy header first) | IN — with zero-auth guard |
| Discord + ntfy webhook pack (2 plugins) | IN |
| Subsonic server_extension | IN (flagship) |
| Per-plugin dep isolation | Design ready, implement after Phase 3 |
| Signed manifests | DEFER (post-Phase-4 optional) |
| Update channels + changelog | DEFER (needs Phase 3) |
| Config-as-code export/import | IN (low effort) |

Implementation order within Phase 4: webhook pack (smallest, proves event
hooks end-to-end) → config-as-code → auth_provider → Subsonic plugin →
dep isolation.
