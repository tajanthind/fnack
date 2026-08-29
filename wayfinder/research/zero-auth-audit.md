# Zero-Auth Audit — fnack (research findings)

**Requirement:** "We should not have a single required authentication by the end."
**Scope:** `app.py` (all routes), `services/lidarr_service.py` (SAB/Newznab emulation + api_key), frontend (`templates/*.html`, `static/app.js`), boot/entrypoint, optional integrations.
**Method:** source inspection (grep for `api_key`, `verify_api_key`, `apikey`, `authentication`, `login`, `before_request`, `login_required`, `HTTPBasicAuth`, `flask_login`, `session[`) — no code modified. Web search unnecessary: the Flask/gunicorn auth story is fully determined by the code (see §6).
**Date:** 2026-08-26 (session).

---

## 1. Verdict (TL;DR)

**No gaps found.** fnack has **zero required authentication** for human-facing use:

- Web UI pages, the first-run flow, and every user-facing `/api/*` endpoint are **completely unauthenticated** — there is no login page, no password, no `before_request` gate, no session/auth middleware anywhere in the app.
- The only credential in the product is the **`api_key`**, which is **auto-generated at first boot**, never entered by a user, never prompted for by the UI, and only enforced on the **machine-to-machine SABnzbd/Newznab/Torznab emulation endpoints** (where an external Lidarr client supplies it from its own config). It is rotatable via `/api/settings/rotate-key` and displayed read-only in Settings → Lidarr Integration. Omitting it never blocks the UI, search, downloads, import, or first-run.
- All optional integrations (VPN, YouTube cookies, Navidrome, Spotify client credentials, planned AcoustID/MusicBrainz) are verified optional with graceful degradation.

Nothing blocks first-run or daily use with an auth wall.

---

## 2. Route inventory (app.py)

### 2.1 Web UI pages — NO auth (all `render_template`, zero checks)

| Route | Handler | Auth |
|---|---|---|
| `/` | `page_index` | none |
| `/artist/<int:artist_id>` | `page_artist` | none |
| `/import` | `page_import` | none |
| `/queue` | `page_queue` | none |
| `/settings` | `page_settings` | none |
| `/health` | `health` | none (JSON) |

### 2.2 User-facing API — NO auth (all of these)

- `GET /api/search-artist`
- `GET /api/artists`
- `GET /api/stats`
- `POST /api/add-artist`
- `GET /api/artist/<id>` · `DELETE /api/artist/<id>` · `POST /api/artist/<id>/set-deezer-id` · `POST /api/artist/<id>/sync` · `POST /api/artist/<id>/monitor` · `POST /api/artist/<id>/download-missing`
- `POST /api/album/<id>/download` · `DELETE /api/album/<id>` · `POST /api/album/<id>/toggle-monitor`
- `POST /api/track/<id>/download` · `DELETE /api/track/<id>` · `POST /api/track/<id>/toggle-monitor` · `POST /api/track/<id>/cancel` · `POST /api/track/<id>/manual-match`
- `GET /api/queue` · `POST /api/queue/retry-failed`
- `POST /api/jobs/<id>/cancel` · `POST /api/jobs/<id>/retry` · `DELETE /api/jobs/<id>`
- `GET /api/import/candidates` · `POST /api/import/folder` · `GET /api/import/bulk/status` · `POST /api/import/folder/bulk`
- `POST /api/navidrome/test` · `POST /api/navidrome/scan` · `POST /api/navidrome/fix-splits`
- `POST /api/maintenance/run`
- `GET /api/vpn/status` · `POST /api/vpn/config` · `POST /api/vpn/start` · `POST /api/vpn/stop` · `DELETE /api/vpn/config`
- `GET /api/cookies/status` · `POST /api/cookies/upload` · `POST|DELETE /api/cookies/delete`
- `GET|POST /api/settings` · `GET /api/version`
- `POST /api/settings/rotate-key`
- Socket.IO `connect` → emits `{"status":"ok"}`, no auth.

### 2.3 Machine-to-machine emulation — `api_key` enforced here only

| Route(s) | Handler | Auth today |
|---|---|---|
| `/api/sabnzbd` · `/api/sabnzbd/api` · `/sabnzbd/api` (GET/POST) | `handle_sabnzbd_api` | **`verify_api_key()` → 401 "Invalid API key"** on every mode |
| `/api/newznab` · `/api/newznab/api` · `/api/torznab` · `/api/torznab/api` · `/torznab/api` (GET) | `handle_newznab_api` | `t=caps` served **without** key (public); all other modes (`t=get`, search) **require** key → 401 |
| `/api/nzb/<item_type>/<int:item_id>` (GET) | `_get_nzb` (via `api_nzb_grab`) | **No key check at all** — serves the NZB to anyone (the search feed embeds `?apikey=` in the link, but the endpoint ignores it) |

---

## 3. The `api_key` — optional, generated, never UI-required

Source: `services/lidarr_service.py` `get_api_key` / `verify_api_key` + `app.py` `/api/settings/rotate-key` + `templates/settings.html`.

- **Generated, never user-supplied:** `get_api_key(app)` auto-creates `secrets.token_hex(16)` on first boot and persists it in the `AppSetting` table (`app.py` line 1641 calls it during startup). There is no setup step, no env var, no first-run prompt.
- **Verification** (`verify_api_key`): compares the `apikey` query param / `X-Api-Key` header / form value against the stored key. Used **only** by the SABnzbd/Newznab/Torznab handlers in §2.3.
- **Rotatable:** `POST /api/settings/rotate-key` (app.py:1466) generates a new key; Settings UI shows a "Rotate API Key" button with confirmation ("You will need to update Lidarr with the new key.").
- **UI is display-only:** Settings → "Lidarr Integration (SABnzbd & Newznab)" shows the key in a **readonly** input with copy + rotate buttons (settings.html:626–655). It is meant to be pasted into **Lidarr's** config (Download Client URL base `/api/sabnzbd`, Indexer URL `/api/newznab`). The UI never asks the user to *enter* the key anywhere.
- **Omitting the key never blocks normal use:** the web UI, all of §2.1, all of §2.2, queue worker, downloads, import, and first-run all work with no key present. Only an external Lidarr/SABnzbd client that omits the key gets a 401 on the emulation endpoints — exactly the intended behavior (the client is configured with the key it was given).
- `GET /api/settings` returns the key in its JSON payload — deliberate for a LAN zero-auth tool so the UI can render it; worth a note in §6 tradeoffs, not a gap for this requirement.

**Classification:** the `api_key` is a **machine-to-machine credential for optional external clients (Lidarr → SABnzbd/Newznab emulation)**. It is *not* a product authentication gate. It satisfies, rather than violates, "no single required authentication": the human-facing product has none.

---

## 4. Optional integrations — all verified optional

| Integration | Evidence | Optional? |
|---|---|---|
| **VPN (OpenVPN/WireGuard)** | `entrypoint.sh:78–115`: tunnel only starts if `/config/vpn/*.ovpn` or `wg0.conf` exists; split-mode keeps dashboard reachable; failures are warnings, never fatal. `/api/vpn/*` endpoints are only invoked from Settings UI. | ✅ Fully optional |
| **YouTube cookies.txt** | `/api/cookies/status` returns a benign "No Cookies Loaded" state (settings.html:858–868); `ytdlp_service.py` has zero-auth resilience (PO-token provider at boot, bot-check/sign-in fallbacks, "cookies.txt authentication" is an optional enhancement, ytdlp_service.py:90,377,540–547). | ✅ Fully optional |
| **AcoustID** | **Not implemented** — no AcoustID code or key field anywhere (grep: only a `TXXX:MusicBrainz` tag strip during MP3 tag writing, `queue_service.py:184`, which is automatic local metadata cleanup, not an auth surface). Tracked as a future ticket (`wayfinder/tickets/acoustid-fingerprinting.md`). | ✅ n/a (planned, no gate) |
| **MusicBrainz** | No MusicBrainz API integration in code (same tag-strip reference only); future ticket (`wayfinder/tickets/musicbrainz-integration.md`). Tagging is automatic and credential-free. | ✅ n/a (planned, no gate) |
| **Navidrome** | Settings default to empty URL/user/token (`/api/settings` GET returns `""`); `navidrome_auto_scan` only acts when configured; `/api/navidrome/test` returns an error message if unreachable but never blocks the app. | ✅ Fully optional |
| **Spotify client id/secret** | Optional; `spotify_service.py` is "zero-auth matching" with oEmbed fallback; client-credentials path only used when the user supplies keys (spotify_service.py:452,485). | ✅ Fully optional |
| **SpotiFLAC** | "Zero-auth lossless" providers installed at startup (`spotiflac_service.py:206`); no credentials required. | ✅ Fully optional |

---

## 5. First-run flow — no credentials anywhere

- No onboarding/login/welcome gate exists: `GET /` renders the dashboard immediately; no redirect to a setup page; no env-var or config requirement for the app to boot (entrypoint.sh only creates `/config /downloads /music` dirs and starts Xvfb/PO-token helpers).
- First boot auto-creates: SQLite DB + indexes, default settings, and the `api_key` (`app.py:1583–1641`) — all background, none user-entered.
- `SECRET_KEY` (Flask sessions) is auto-generated and persisted to `/config/secret_key`; it gates nothing (no session-based auth exists).
- Nothing in first-run or daily use requires credentials. **No flags.**

---

## 6. Server layer (dev vs production)

- **Production:** `Dockerfile` CMD runs **gunicorn** with the geventwebsocket worker (`gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 -b 0.0.0.0:4688 --timeout 300 ... app:app`); confirmed live in `fnack-logs.txt` ("Starting gunicorn 26.1.0"). Gunicorn applies **no HTTP authentication by default** and none is configured (no `--user`, no auth module).
- **Dev:** `python app.py` runs `socketio.run(app, ..., allow_unsafe_werkzeug=True)` (app.py:1680) — the Werkzeug dev server. It adds **no basic auth**; the flag only silences the "unsafe debugger" warning and matters only for the Werkzeug debugger (debug is off). Both paths are unauthenticated.
- Conclusion: there is no HTTP-level auth layer at any deployment tier; the zero-auth property is app-level and holds under gunicorn.

---

## 7. Audit table

| Surface | Auth today | Required? | Action |
|---|---|---|---|
| `/` (dashboard) | none | **No** | none — keep |
| `/artist/<id>` | none | **No** | none — keep |
| `/import` | none | **No** | none — keep |
| `/queue` | none | **No** | none — keep |
| `/settings` | none | **No** | none — keep |
| `/health` | none | **No** | none — keep |
| Search/onboarding API (`/api/search-artist`, `/api/artists`, `/api/stats`, `/api/add-artist`) | none | **No** | none — keep |
| Artist/album/track actions (`/api/artist/*`, `/api/album/*`, `/api/track/*`) | none | **No** | none — keep |
| Queue/jobs API (`/api/queue`, `/api/queue/retry-failed`, `/api/jobs/*`) | none | **No** | none — keep |
| Import API (`/api/import/*`) | none | **No** | none — keep |
| Navidrome API (`/api/navidrome/*`) | none (integration creds optional) | **No** | none — keep |
| Maintenance (`/api/maintenance/run`) | none | **No** | none — keep |
| VPN API (`/api/vpn/*`) | none | **No** | none — keep (VPN itself optional) |
| Cookies API (`/api/cookies/*`) | none | **No** | none — keep (cookies optional) |
| Settings API (`GET/POST /api/settings`) | none | **No** | none — keep |
| Rotate key (`POST /api/settings/rotate-key`) | none | **No** | none — keep (key is M2M, optional) |
| Socket.IO events | none | **No** | none — keep |
| `/api/sabnzbd`, `/api/sabnzbd/api`, `/sabnzbd/api` (SABnzbd emulation) | `api_key` (401 without) | **Only for external Lidarr/SABnzbd M2M clients** | none — keep as-is; optional for the product |
| `/api/newznab`, `/api/torznab`, `*/api` (Newznab/Torznab emulation) | `api_key` (401 without; `t=caps` public) | **Only for external indexer clients** | none — keep as-is; optional for the product |
| `/api/nzb/<type>/<id>` (NZB download link) | **none** (key accepted but not enforced) | **No** | optional hardening: could enforce key for parity with SAB/Newznab, but this is M2M-only and does not affect zero-auth requirement |

---

## 8. Gaps / flags

- **None block first-run or daily use.** The product has zero required authentication end-to-end.
- Minor observations (not gaps against the requirement, recorded for awareness):
  1. `/api/nzb/<type>/<id>` skips the key check the other M2M endpoints enforce — harmless for the requirement (it's an M2M download link), but a one-line parity hardening if desired.
  2. `GET /api/settings` exposes the `api_key` to any LAN caller — inherent to the zero-auth design (same as the rest of the UI); acceptable for a self-hosted LAN tool, worth remembering if the deployment is ever exposed publicly.
  3. Socket.IO has `cors_allowed_origins="*"` — a zero-auth product on a LAN; not an auth gate, noted for completeness.
- If a future "optional auth" toggle is ever added (e.g., reverse-proxy basic auth), it must remain off by default and non-blocking to preserve this property.
