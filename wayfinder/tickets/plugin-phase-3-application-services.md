# Phase 3: Application Services, Verification, and Queue decoupling

wayfinder:phase-3

## What this covers (03-PHASE-3-APPLICATION-SERVICES-VERIFICATION-AND-QUEUE.md)

Make `queue_service.py` an orchestrator instead of a provider implementation
hub. It must not know Spotify, Deezer, AcoustID, SpotiFLAC, yt-dlp, or
Navidrome. The capability registry + application services own provider
resolution; core calls capabilities only.

(NOTE: the older `plugin-phase-3-design.md` ticket + Phase 3 "marketplace"
roadmap entry are a DIFFERENT, already-landed marketplace phase; the new
brief renumbers Phase 3 as the application-services layer. Map entry below
records this.)

## Target services (each = its own PR, per handoff PR discipline 05 §PR discipline)

1. **DownloadService** (`services/download_service.py`) — PR 5
   `async download(request: DownloadRequest) -> DownloadResult`; resolves
   `download.track`; tries providers per download policy (sequential
   fallback, rate-limit skip, can_handle gate); **no providers → raise
   `CapabilityUnavailable("download.track", "download_track")`**; no hidden
   provider fallback. Queue's `_process_track_job` + manual path call this.
2. **MetadataService** (`services/metadata_service.py`) — PR 6
   `resolve_track_url` / `search_artist` / `get_artist_discography` /
   `get_track_metadata` / `get_album_metadata`; each resolves a capability
   (track.resolve, artist.search, artist.discography, track.metadata,
   album.metadata); NO Spotify/Deezer/iTunes imports. Callers today:
   app.py (search_artist/get_artist_discography/get_artist_info routes),
   queue_service (get_track_info ISRC/genre auto-resolve),
   import_service (search_artist/get_artist_discography), plugins/context.py
   (facade methods — keep, they're the plugin boundary).
3. **FingerprintService** (`services/fingerprint_service.py`) — PR 7
   `async identify(request: FingerprintRequest) -> list[FingerprintEvidence]`;
   discover providers, invoke, normalize errors, enforce timeout, run
   concurrently where practical; does NOT decide whether the download is
   valid. Semantics: provider no_match → no evidence; mismatch → negative
   evidence; timeout → provider error; unavailable → unavailable. Never treat
   a missing fingerprint result as proof of a mismatch.
4. **VerificationService** (`services/verification_service.py`) — PR 8
   `async verify(expected: TrackRef, file_path: Path) -> VerificationResult`;
   combines ISRC/artist/title/album/duration + fingerprint evidence;
   **provider-neutral** — no `if acoustid_match:` in core, only normalized
   evidence. `VerificationResult` dataclass: status
   (verified/mismatch/uncertain/provider_error), score, reasons,
   metadata_evidence, fingerprint_evidence, canonical_match.
5. **MediaServerService** (`services/media_server_service.py`) — PR 9
   `scan` / `health` / `test_connection`; resolves media.scan / media.health
   / media.connection_test; must not know Navidrome. Callers today:
   app.py (test_navidrome_connection/trigger_navidrome_scan routes),
   queue_service (trigger_navidrome_scan after downloads).
6. **Queue/API cleanup** — PR 10: queue flow toward
   `download = await download_service.download(request)` →
   `verification = await verification_service.verify(...)` → verified →
   finalize / else → failure handling. Do NOT rewrite unrelated queue
   behavior. API routes call application services
   (`metadata_service.search_artist`, `media_server_service.scan`), not
   `services.deezer_service...` / `services.navidrome_service...`.
7. **Candidate configuration** — PR 10/11: provider connection tests
   support unsaved settings (`await provider.test_connection(candidate_config)`)
   — removes the justification for direct core provider-service access.

## Completion criteria (brief §Completion criteria)

- queue has no provider imports; queue has no provider IDs
- metadata is capability-based; fingerprinting is capability-based;
  verification is provider-neutral; media operations are capability-based
- API routes use application services
- zero providers produces structured unavailable results
- multiple providers work; provider errors do not crash the queue

## Sequencing notes (interleaving with remaining provider extractions)

- DownloadService can land NOW (download.track already has SDK providers
  spotiflac + ytdlp; no new extraction needed).
- MetadataService/FingerprintService/MediaServerService build on the
  capability registry; the metadata/fingerprint/media plugins are still
  wrappers over legacy services — the service layer resolves capabilities,
  so it works today and improves as extractions land (handoff: "plugin ->
  legacy core service" acceptable temporarily, final state is core ->
  capability -> plugin implementation).
- Remaining Phase-2 extractions (spotify, deezer, musicbrainz+itunes,
  acoustid, navidrome) proceed on their own branches in parallel per the
  handoff PR list; each service PR is independent.

## Per-PR checklist (standing workflow)

one branch+PR per step; PR only (never merge/tag/bump version.py); smoke +
architecture tests green; live-boot verify; wayfinder + docs updated;
fnack-plugins synced when a provider's impl moves.

## Progress

- **Step 1 DONE (PR 5 = DownloadService)**: `services/download_service.py`
  — DownloadService owns download.track resolution + policy (rate-limit
  skip, can_handle gate, sequential fallback, optional verify hook with
  accept/flag/reject verdicts, stop_on_first_attempt for the manual path);
  zero providers -> CapabilityUnavailable("download.track",
  "download_track"). The provider-invocation adapter helpers
  (_is_sdk_downloader/_build_download_request/_invoke_downloader_can_handle/
  _invoke_downloader_download) moved from queue_service into the service and
  now return the FINAL SDK DownloadResult shape (legacy providers normalized
  up). queue_service: _process_track_job builds a DownloadRequest and calls
  DownloadService().download(request, verify=_verify_hook,
  on_progress=_on_progress); the manual-path helpers (_download_via_chain /
  _download_via_ytdlp_provider) delegate to DownloadService with
  stop_on_first_attempt=True. Parity test
  tests/architecture/test_download_service.py (resolution, zero-provider
  CapabilityUnavailable, fallback, verify accept/flag/reject,
  stop_on_first_attempt, queue-delegates source check). Smoke + 9
  architecture tests green; live-boot verified (providers resolve,
  manual path fails cleanly, zero-provider raises). Note: queue still
  imports deezer/spotify/verifier/navidrome/acoustid for metadata/
  verification/media — those are Steps 2–4.
- **Step 2 DONE (PR 6 = MetadataService)**: `services/metadata_service.py`
  (new — the old tag-normalization module moved to
  services/tag_normalization_service.py, 2 script imports updated).
  MetadataService resolves the metadata capabilities — track.resolve /
  artist.search / artist.discography / track.metadata / album.metadata —
  via the registry (priority-ordered, enabled only), first-non-empty policy,
  manager-boundary invocation, CapabilityUnavailable per capability when
  zero providers (no hidden fallback). get_artist_discography forwards
  filter kwargs only to providers that accept them (signature inspection).
  Callers migrated: app.py api_search_artist / artist sync / api_artist_sync
  and import_service (search + discography) call MetadataService; the
  queue's ISRC/genre auto-resolve (get_track_metadata), resolve_track_url
  and manual-path Deezer-URL handling no longer import deezer/spotify
  services. app.py keeps get_artist_info direct (no capability exists —
  onboarding-only, allowlist updated). plugins/context.py get_album_info /
  get_track_info route through MetadataService. fnack.spotify on_load
  migrates legacy spotify_client_id/secret; fnack.deezer-batch exposes
  get_album_info (declares album.metadata) and accepts **filters — synced to
  fnack-plugins + repackaged. Parity test
  tests/architecture/test_metadata_service.py (provider-neutral source,
  zero-provider CapabilityUnavailable per method, first-non-empty policy,
  filter forwarding, caller migration, module rename). Smoke + 10
  architecture tests green; live boot verifies real Deezer search / Spotify
  URL resolution / Deezer track metadata through the capability chain and
  the zero-provider structured error.
- **Step 3 DONE (PR 7 = FingerprintService + VerificationService)**:
  `services/fingerprint_service.py` resolves fingerprint.identify providers
  (fnack.acoustid today) via the registry, invokes through the manager
  boundary (timeout + auto-disable), normalizes SDK FingerprintEvidence /
  legacy FingerprintResult into evidence; provider error -> error evidence
  (never crash); provider no_match -> NO evidence (missing fingerprint never
  a mismatch). `services/verification_service.py` combines metadata evidence
  (duration + tags via generic core verify_audio_file) with fingerprint
  evidence into provider-neutral VerificationResult (verified/mismatch/
  uncertain/provider_error + score/reasons/evidence/canonical_match); the
  SERVICE compares matched identity vs expected (all present fields must
  agree — faithful to the legacy candidate cross-check) — no acoustid branch
  in core. queue `_verify_or_rescue` routes through VerificationService (no
  acoustid_service import remains in the queue). New SDK models:
  MetadataEvidence / TrackMatch / VerificationResult. Parity test
  tests/architecture/test_verification_service.py (provider-neutral source,
  zero-provider CapabilityUnavailable, evidence normalization both shapes,
  agree->verified / contradict->mismatch / no-evidence->uncertain /
  provider-error, queue-routes-through-service). Smoke + 11 architecture
  tests green; live boot verifies acoustid provider resolution, no-key
  identify returns no evidence, verify returns structured results.
- **Step 4 DONE (PR 8 = MediaServerService)**: `services/media_server_service.py`
  resolves media.scan / media.health / media.connection_test via the
  capability registry (fnack.navidrome today), first-success policy, zero
  providers -> CapabilityUnavailable per method; the service never names
  Navidrome. Candidate configuration: test_connection(candidate_config)
  forwards UNSAVED settings to providers that accept them (signature
  inspection) — the settings UI validates a typed-but-not-saved config
  through the application service, removing the direct core provider-service
  access the old route justified. fnack.navidrome plugin gained
  test_connection(candidate_config=...) + health() and declares
  media.health (synced + repackaged). Callers migrated: app.py
  /api/navidrome/test + /api/navidrome/scan routes and queue post-download
  auto-scans route through the service (run_auto_split_repair split-repair
  task has no capability yet — stays transitional; independence allowlist
  updated). Parity test tests/architecture/test_media_server_service.py
  (provider-neutral source, zero-provider CapabilityUnavailable,
  scan/health first-success, candidate-config forwarding both provider
  shapes, caller migration). Smoke + 12 architecture tests green; live boot
  verifies scan/test/health through the capability chain, zero-provider
  structured error, and the scan route.
- **Step 5 DONE (PR 9 = Queue/API cleanup)**: the queue is a pure
  orchestrator — imports only generic core (verifier_service, models,
  requests) plus the four application services; zero provider imports, zero
  provider-ID branches (verified at source level). API routes use application
  services; the only remaining app.py provider imports are the documented
  transitional ones (musicbrainz enrich, acoustid manual-identify, navidrome
  fix-splits — no capability in the MASTER set). New
  tests/architecture/test_phase3_completion.py asserts ALL brief completion
  criteria: queue provider-free, queue orchestrates through the services,
  app routes use services, zero providers -> CapabilityUnavailable per
  service, multiple providers work, provider errors never crash the queue.
  Smoke + 13 architecture tests green; live boot confirms all capabilities
  resolve and routes serve through the services. PHASE 3 COMPLETE.
