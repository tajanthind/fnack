# Phase 2 pipeline cutover — design findings

Status: research (no code changed). Companion ticket:
`wayfinder/tickets/plugin-phase-2-design.md`. Branch: implementation happens
on `plugin-architecture/phase-2-pipeline-cutover` after PR #2 merges.

Scope: exact replacement for the hardcoded downloader + metadata sequences in
`queue_service._process_track_job` and the sync/import orchestration, using the
Phase 1 plugin chains. Behavior-preserving.

---

## 1. The current downloader sequence (what we're replacing)

In `services/queue_service.py` `_process_track_job` (~lines 676–780), today:

1. **Spotify URL resolution** (core, stays): `resolve_spotify_url(...)` →
   `spotify_url` (None if disabled/no credentials). Gates SpotiFLAC.
2. **Primary: SpotiFLAC** (`enable_spotiflac` setting + `spotify_url` + not
   rate-limited): if `is_spotiflac_rate_limited()` → failure_reasons append
   `"SpotiFLAC skipped (upstream rate limit circuit breaker)"`; else
   `download_track_spotiflac(spotify_url, tmp_work_dir, quality=quality_setting,
   rate_limit_delay=spotiflac_delay)` → on success `_verify_or_rescue(...)` →
   verified/flagged/failure. Progress 35.0 emitted before the attempt.
3. **Fallback: yt-dlp** (`enable_ytdlp` setting, no verified file yet):
   `download_track_ytdlp(f"{artist} - {title}", tmp_work_dir,
   output_format=fallback_format, artist_name=..., track_title=...,
   expected_duration=..., cookies_path=..., prefer_youtube_music=...,
   max_duration_delta=..., check_duration=...)` → on success `_verify_or_rescue`
   → verified/flagged/failure. Progress 60.0 emitted before the attempt.
4. If neither ran (both disabled): failure_reasons append
   `"Both SpotiFLAC and yt-dlp engines are disabled in settings"`.

The `enable_spotiflac` / `enable_ytdlp` settings are the *enable toggles*; the
Phase 1 plugins replaced the services but the hardcoded sequence is still the
code path (Phase 1 did NOT cut over — that's this phase).

---

## 2. The new downloader chain (INTEGRATION.md §6 + this design)

Replace Steps 2–4 with a priority-ordered loop over
`plugin_manager.get_downloaders()` (already sorted by effective priority:
spotiflac p10 → ytdlp p50). **Per-downloader verify inside the loop**
(option (a) — preserves today's exact behavior, including which error string
surfaces and the AcoustID rescue per engine).

### 2.1 TrackRef construction (before the loop)

```python
from plugins.base import TrackRef

track_ref = TrackRef(
    id=track.id,
    title=track_title,
    artist_name=artist_name,
    album_name=album_name,
    isrc=isrc,
    duration=expected_duration,
    spotify_url=spotify_url,       # resolved in Step 1 (core, unchanged)
    deezer_id=track.deezer_id if track else None,
    disc_number=disc_num_val or 1,
    track_number=track_num,
)
```

### 2.2 The loop (replaces Step 2 + Step 4)

```python
from plugins.manager import plugin_manager

downloaders = plugin_manager.get_downloaders() if plugin_manager else []
enabled_any = bool(downloaders)
for dl in downloaders:
    if job_id in cancel_requested_jobs:
        break
    if dl.is_rate_limited():
        logger.info("[QUEUE] %s rate-limited; trying next downloader for '%s - %s'",
                    dl.manifest.id, artist_name, track_title)
        failure_reasons.append(f"{dl.manifest.name} skipped (rate limited)")
        continue
    if not dl.can_handle(track_ref):
        continue
    loaded = plugin_manager._plugins[dl.manifest.id]
    options = _downloader_options(dl)   # see §2.3
    socketio.emit("download_progress", {"job_id": job_id, "track_id": track_id,
                                        "progress": 35.0, "status": "downloading"})
    result = plugin_manager.call_safe(loaded, "download", track_ref, tmp_work_dir, options)
    if result and result.success and result.file_path:
        downloaded_file = result.file_path
        v_ok, v_err, meta, flagged = _verify_or_rescue(
            app, downloaded_file, verify_expected_duration,
            artist_name, track_title, max_duration_delta,
            reject_mismatches, enable_duration_check,
        )
        if v_ok:
            verified_file = downloaded_file
            file_meta = meta
            break
        if flagged:
            flagged_caution = flagged
            verified_file = downloaded_file
            file_meta = meta
            break
        failure_reasons.append(f"{dl.manifest.name} verification failed: {v_err}")
    else:
        err = (result.error if result else "download returned no result")
        failure_reasons.append(f"{dl.manifest.name} failed: {err}")
        # On success-path break: circuit breaker state already updated inside
        # the plugin's underlying service (module globals preserved).

if not verified_file and not enabled_any:
    failure_reasons.append("No downloader plugins are enabled in Settings → Plugins")
```

Notes:
- **Rate-limit skip message**: `fnack.spotiflac.is_rate_limited()` is the same
  429 circuit breaker as before; the message text is intentionally similar but
  names the plugin ("SpotiFLAC skipped (rate limited)") — flag this as the one
  intentional wording change (PHASE1 §6.4 callout), since the old string said
  "upstream rate limit circuit breaker". If the user wants the exact old text,
  keep it in the spotiflac plugin's `is_rate_limited` docstring instead.
- **Progress %**: emit 35.0 for the first (primary) and 60.0 for fallbacks to
  preserve the UI feel — simplest correct mapping: emit 35.0 before the first
  plugin in the loop, 60.0 before subsequent ones.
- **All-disabled message** replicates the old "Both engines disabled" intent
  with plugin-aware wording.

### 2.3 `_downloader_options(dl)` mapping table

| Plugin | option key | source (priority) | default |
|---|---|---|---|
| fnack.spotiflac | `quality` | settings `quality` → AppSetting `spotiflac_quality` | `LOSSLESS` |
| fnack.spotiflac | `delay` | settings `delay` → AppSetting `spotiflac_delay` | `3.0` |
| fnack.ytdlp | `format` | settings `format` → AppSetting `ytdlp_format` | `opus` |
| fnack.ytdlp | `audio_source` | settings `audio_source` → AppSetting `ytdlp_source` | `youtube_music` |
| fnack.ytdlp | `cookies_path` | settings `cookies_path` → `get_cookies_path(...)` | `/config/cookies.txt` |

The plugins already read their own settings; the queue passes the legacy
AppSetting values as fallback so nothing changes for existing users.

---

## 3. Metadata chain cutover

Today (`app.py:_sync_artist_discography_background` + `import_service`):
`deezer_service.get_artist_discography(...)` then
`musicbrainz_service.enrich_albums(...)`; iTunes used elsewhere as fallback.

New shape (Phase 2):

```python
from plugins.base import TrackRef  # n/a — metadata uses names, not TrackRef

for provider in plugin_manager.get_metadata_providers():   # deezer p10 → mb p20 → spotify p30 → itunes p40
    if provider.manifest.id == "fnack.deezer-batch":
        disco = provider.get_artist_discography(str(deezer_artist_id))
        if disco and disco.get("albums"):
            break
    # MusicBrainz enrichment stays core glue: after the Deezer discography is
    # fetched, call enrich via the chain's musicbrainz plugin (exposes .enrich).
# MusicBrainz: enrichment-only, called AFTER the authoritative Deezer fetch
for provider in plugin_manager.get_metadata_providers():
    if provider.manifest.id == "fnack.musicbrainz":
        provider.enrich(disco.get("artist_name") or artist.name, disco.get("albums") or [])
        break
```

- **MusicBrainz pacing preserved**: `enrich_albums` lives in
  `musicbrainz_service.py` with MIN_INTERVAL=1.0 + Retry-After + negative cache;
  the plugin's `enrich()` calls it directly — the pacing is unchanged.
- Interactive `/api/search-artist` stays core (Deezer direct) — untouched.

---

## 4. Behavior-preservation checklist

| Current behavior | Chain equivalent | Preserved? |
|---|---|---|
| SpotiFLAC tried first (priority) | chain order spotiflac p10 → ytdlp p50 | ✅ |
| 429 breaker skips SpotiFLAC | `is_rate_limited()` skip in loop | ✅ (wording tweak flagged) |
| verify right after each engine | `_verify_or_rescue` inside loop per plugin | ✅ |
| AcoustID rescue (flag, don't delete) | `_verify_or_rescue` unchanged | ✅ |
| progress 35/60 | emit before first / subsequent plugins | ✅ |
| enable_spotiflac/enable_ytdlp toggles | plugin enable/disable in Settings→Plugins | ✅ (disabled plugin skipped) |
| "Both engines disabled" message | "No downloader plugins are enabled..." | ✅ (wording updated) |
| failure_reasons strings | built from `dl.manifest.name` | ⚠ names change ("SpotiFLAC"→same, "yt-dlp"→same) — verify text matches old exactly where it matters |
| resolve_spotify_url before chain | Step 1 unchanged (core) | ✅ |
| metadata: Deezer authoritative → MB enrich | provider chain p10 deezer → mb enrich glue | ✅ |

Top 5 risks:
1. **Error-string drift** — failure_reasons text shown in UI/history must read
   identically; map `dl.manifest.name` to the old engine names ("SpotiFLAC",
   "yt-dlp") rather than plugin ids.
2. **Rate-limit skip wording** — the old "upstream rate limit circuit breaker"
   string; keep it unless user approves the tweak.
3. **can_handle gates** — spotiflac needs `spotify_url`; yt-dlp needs a title.
   If a track lacks both (edge), the chain yields nothing → must hit the
   all-failed message, same as today's fall-through.
4. **Verify-per-engine vs verify-once** — must stay per-engine to preserve the
   rescue semantics and which failure surfaces first.
5. **Settings fallback** — any plugin setting not yet migrated must fall back to
   the legacy AppSetting row, or behavior changes for existing users.

---

## 5. Summary

Phase 2 = one focused change in `_process_track_job` (Steps 2–4 → the
`get_downloaders()` loop with per-engine verify) + one in the sync/import
metadata calls (`get_metadata_providers()` chain + MB enrich glue). Everything
else (Spotify resolution, `_verify_or_rescue`, AcoustID rescue, progress
emissions, settings sources) stays put. The design is deliberately minimal:
the Phase 1 plugins are already the exact services behind the same interfaces,
so the cutover is a call-site swap, not a behavior change.
