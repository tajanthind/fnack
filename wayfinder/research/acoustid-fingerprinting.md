# Optional AcoustID fingerprinting — research findings

Research for ticket `tickets/acoustid-fingerprinting.md` (wayfinder:research).
No code was modified. The existing duration + tag verifier
(`services/verifier_service.py`) remains authoritative; AcoustID is a strictly
optional bonus signal.

Sources: [AcoustID Web Service docs](https://acoustid.org/webservice),
[AcoustID FAQ](https://acoustid.org/faq),
[fpcalc(1) man page (bookworm)](https://manpages.debian.org/bookworm/libchromaprint-tools/fpcalc.1),
[libchromaprint-tools package (bookworm)](https://packages.debian.org/bookworm/libchromaprint-tools),
[libchromaprint1 package (bookworm)](https://packages.debian.org/bookworm/libchromaprint1),
[pyacoustid README](https://pypi.org/project/pyacoustid/1.1.0/),
[beetbox/pyacoustid source](https://github.com/beetbox/pyacoustid).

---

## 1. Dependency / key facts

### The AcoustID web service (what fnack would call)

- One endpoint matters for us: **lookup by fingerprint** —
  `GET/POST https://api.acoustid.org/v2/lookup`. (Submit is the other operation;
  fnack does not need it — see Open Questions.)
- Params (from the [Web Service docs](https://acoustid.org/webservice)):
  - `client` — **required**. The *application API key*, obtained by registering
    the application for free at acoustid.org. This is not a user account and
    introduces **zero required auth** on fnack's side: no key configured =
    fingerprinting simply disabled, everything else works.
  - `duration` — **required**. Duration of the *whole audio file* in seconds
    (not just the fingerprinted window). fpcalc prints this.
  - `fingerprint` — **required**. The Chromaprint fingerprint string.
  - `meta` — optional: `recordings, releases, releasegroups, isrcs, compress, …`.
    `recordings+releasegroups+isrcs+compress` gives everything fnack needs.
  - `format` — optional; JSON is the default.
- Response shape (verified against the docs' example):
  ```json
  { "status": "ok",
    "results": [ { "id": "<acoustid track id>", "score": 1.0,
      "recordings": [ { "id": "<musicbrainz recording MBID>",
        "title": "Lower Your Eyelids to Die With the Sun",
        "artists": [ { "id": "<mbid>", "name": "M83" } ],
        "duration": 639,
        "releasegroups": [ { "type": "Album", "id": "<mbid>", "title": "…" } ],
        "isrcs": [ "GB…" ] } ] } ] }
  ```
  The database is crowd-sourced: some recordings have no `title`/`artists`;
  regional artists are often absent entirely (a lookup that finds nothing is a
  **normal, expected outcome** for this library).
- `score` is a float **0–1 fingerprint similarity** (1.0 = exact match), not a
  statistical confidence. It is the ranking signal; a metadata cross-check is
  still required before fnack *acts* on it (see Thresholds).
- Rate limiting / usage: the [usage guidelines](https://acoustid.org/webservice)
  ask for **no more than 3 requests/second** and non-commercial use only —
  both trivially satisfied by fnack (single worker, max_concurrent defaults to
  1). pyacoustid implements the same 3/s throttle internally.
- API key types: `client` key = application key (what fnack needs, free
  registration). The *user* key (after login) is only for **submitting**
  fingerprints — not needed for lookup.
- MusicBrainz API is **not required**: AcoustID returns recording
  title/artists/releases inline. The MusicBrainz web service would only be
  needed for richer data (extra artists, release groups, exact year) and has
  its own 1 req/s + User-Agent rules — avoid it in v1.

### Fingerprint generation: `fpcalc` in Debian bookworm

- The `python:3.11-slim` base does **not** ship chromaprint. Debian bookworm
  provides **`libchromaprint-tools` 1.5.1-2**, which contains the `fpcalc`
  binary ([package page](https://packages.debian.org/bookworm/libchromaprint-tools)).
- Its deps are `libchromaprint1`, `libavcodec59`, `libavformat59`,
  `libavutil57`, `libswresample4`, `libstdc++6` — **all already installed** in
  the fnack image via the existing `ffmpeg` Debian package in the Dockerfile.
  Net new image weight: `fpcalc` + `libchromaprint1` (~a few hundred KB). Add
  one package to the existing `apt-get install` line in `Dockerfile`.
- `fpcalc` behavior ([man page](https://manpages.debian.org/bookworm/libchromaprint-tools/fpcalc.1)):
  `fpcalc [-length SECS] FILE` prints two lines: `DURATION=<secs>` (whole file)
  and `FINGERPRINT=<base64>`. **`-length` defaults to 120** — it fingerprints
  only the first 120 s of audio, which is the right window for whole-track
  verification and matches AcoustID's "full audio file" design (the
  [FAQ](https://acoustid.org/faq) explicitly says short snippets are not
  supported). Decoding uses libavformat, so every format fnack produces
  (FLAC/MP3/M4A/Opus/OGG/WAV) is supported.
- Alternative: pip **`pyacoustid`** (ctypes bindings). It needs the
  `libchromaprint1` `.so` *or* `fpcalc` on PATH, and uses `audioread` to decode
  (finds the already-installed `ffmpeg` binary). It bundles the 3/s throttle
  and gives `acoustid.match(apikey, path) -> (score, mbid, title, artist)`.
- **Recommendation: fpcalc subprocess + `requests`** (already a dependency).
  Matches fnack's existing "downloads run as subprocesses" pattern
  (`ytdlp_service`, `spotiflac_service`), adds zero new pip packages, and
  fpcalc supplies `DURATION` for the lookup for free. pyacoustid remains a fine
  fallback if pure-Python is preferred later.

### Cost of running fpcalc per file

- fpcalc decodes ~120 s of audio at faster-than-realtime and Chromaprint itself
  is a few tens of ms of compute. Realistic budget: **~0.2–1.5 s CPU per
  typical 3–4 min file** (I/O + decode bound), plus ~50 ms process spawn.
  Against a download that takes seconds-to-minutes this is negligible, but it is
  not free — which is why it should not run on every download by default
  (see below).
- Lookup HTTP call: ~100–300 ms; well inside the 3 req/s guideline.

---

## 2. Verification flow (download time) — when + cost

**Principle:** the existing verifier (`verify_audio_file`: duration delta +
embedded-tag containment, reject only on confirmed mismatch) is unchanged and
authoritative. AcoustID *confirms* or *overrides* in the narrow case where the
verifier is unsure or the file is about to be deleted.

**Key handling:** a new optional `acoustid_api_key` setting (env
`ACOUSTID_API_KEY` → settings page → `AppSetting`, alongside the existing
settings mechanism in `app.py`). Empty/absent = feature off; nothing in the
download path even checks for it.

**When to run — default: NOT every download.** Fingerprint only when the
verifier is unsure or the download failed:

1. **Verifier rejected (tag mismatch / duration mismatch) and `reject_mismatches`
   is on — i.e. the file is about to be deleted.** Before deleting, fingerprint
   the file and look it up:
   - Top AcoustID result passes the confirm gate (score ≥ 0.8 **and** recording
     title containment-matches the expected track **and** recording duration
     within tolerance) → the file **is** the right song with wrong/absent tags →
     **accept it** (fnack's finalize step `_tag_audio_file` already rewrites
     tags with the expected metadata, so the file is fixed in place). This
     targets exactly the 24 "verifier tag-mismatch rejections" in the failure
     breakdown (wrong tags on a right file).
   - Fingerprint confirms a *different known* recording (fails the cross-check)
     → reject exactly as today (delete), same error path.
   - **No AcoustID match (regional artist) → silent no-op:** fall back to the
     existing verdict. No extra error, no retry, no UI change.
2. **Verifier passed but tags were empty/unverifiable** (only duration was
   checked) → optional confirmation. Default: skip (unverifiable-but-duration-ok
   already passes today); expose as a per-download or setting-driven option.
3. **Download failed with no file** → nothing to fingerprint; AcoustID cannot
   help (no audio exists). No change.
4. Optional setting `acoustid_verify_every` (default **off**) for users who want
   fingerprint confirmation on every successful download. Cost: ~0.2–1.5 s
   fpcalc + one HTTP lookup per file; at single concurrency and 3 req/s this is
   fine, but it is a per-file tax with little benefit once the verifier already
   passed.

**Cost summary:** fpcalc only runs on the rare trigger paths (verifier
rejection / explicit request) — ~0.2–1.5 s + one lookup each, with a 3/s
request budget that single-worker fnack can never exceed.

---

## 3. Identification flow (unknown / weak-tag file)

**UI:** extend the existing **Manual Match / Fix Song** modal
(`static/app.js` `openManualMatchModal`, track-row search icon) with a secondary
**"Identify this file"** action. The button is rendered **only when a key is
configured** (settings GET already returns all settings; expose
`acoustid_configured`). For files with a `local_path` (downloaded or imported).

**Flow** (new `POST /api/track/<id>/identify`, background task like the manual
match handler):

1. `fpcalc <local_path>` → `(duration, fingerprint)` (fpcalc failure = treated
   as no-match, see §4).
2. `GET api.acoustid.org/v2/lookup` with `client`, `duration`, `fingerprint`,
   `meta=recordings+releasegroups+isrcs+compress`.
3. Parse `results[].recordings[]`, dedupe by recording MBID, keep the top
   **5** candidates with **score ≥ 0.4**.
4. Render a picker in the modal: `Title — Artist (Album) · score 0.9 · 3:12`,
   sorted by score.

**On pick, two actions:**

- **Fix tags only** — rewrite the local file's tags with the candidate
  metadata (reuse `_tag_audio_file`). Fixes wrong/empty-tag files without any
  download.
- **Download this version** — AcoustID returns no URL, so this reuses fnack's
  existing download machinery:
  - If the candidate has an **ISRC** (`meta=isrcs`) →
    `resolve_spotify_url(song_name, artist_name, isrc=candidate_isrc)` →
    SpotiFLAC (lossless, ISRC is the strongest zero-auth signal) → yt-dlp
    fallback, exactly like today's pipeline.
  - No ISRC → the standard artist+title search path
    (`resolve_spotify_url` → SpotiFLAC, then `download_track_ytdlp`).
  - Implementation: extend `download_manual_match_track(app, socketio,
    track_id, url)` to accept an optional candidate payload
    `{title, artist, isrc}` that bypasses the URL branch and feeds the same
    verify → tag → finalize pipeline. The manual-match endpoint already proves
    this flow; candidate-based matching is its sibling.
  - The candidate's recording MBID can be stored on the Track for later
    MusicBrainz enrichment.

**No match** → the modal shows "No fingerprint match found — this track isn't in
the AcoustID database." Non-blocking; the track keeps its state and the user can
still paste a URL.

---

## 4. Regional / no-match handling (must be a silent no-op)

- **No AcoustID results anywhere in the flow = silent no-op.** The existing
  verifier verdict stands, downloads proceed or are rejected exactly as today;
  no error state, no UI blocking, no retry storm. One debug-level log line.
- **fpcalc failure** (corrupt/unrecognized audio) → same as no-match.
- **API failure** (missing key, 403, 429, 5xx, network) → treat as no-match for
  verification; for the identification UI show "identification unavailable".
  Never affects the download path. A tiny token-bucket (or fixed 0.4 s spacing)
  throttle honors the 3 req/s guideline if `requests` is used directly.
- The manual-match/identify UI stays functional with no key — the "Identify
  this file" button simply isn't rendered.

---

## 5. Thresholds

- `score` is fingerprint similarity (0–1), not statistical confidence → always
  combine with a metadata cross-check before fnack *acts*.
- **Confirm gate (download verification, auto-accept an otherwise-rejected
  file):** top AcoustID result with
  - `score ≥ 0.8`, **and**
  - recording `title` normalized-containment of the expected title (reuse
    `_check_title`; also run `_check_variant` so a high-score *remix/live/cover*
    fingerprint cannot auto-accept), **and**
  - recording `artists` matches the expected primary artist OR the title check
    already passed (`_check_artist`-style logic), **and**
  - `|recording.duration − expected_duration| ≤ max_duration_delta`.
  Passing all four = the file is the right song despite bad tags → accept and
  let the finalize step retag it. Anything else = existing verdict stands.
- **Definitive wrong-song confirmation:** top result score ≥ 0.8 *and* the
  cross-checks fail against the expected track → the existing rejection is
  confirmed (it was already going to reject; AcoustID only strengthens the
  log).
- **Identification display:** candidates with `score ≥ 0.4`, top 5, ranked by
  score; **never auto-accepted** — the user picks.
- Scores between 0.4–0.8 are shown as "possible match, verify" in the picker,
  never used for auto-confirmation.

---

## 6. Open questions

1. **Override semantics:** should AcoustID confirmation *override* a
   tag-mismatch rejection by default (my recommendation — it catches exactly the
   "right file, wrong tags" failures), or only behind an explicit setting
   (`acoustid_override_verifier`)? The ticket says the verifier is authoritative
   — the user should confirm the override default. The confirm gate (§5) is
   deliberately strict so this is safe either way.
2. **Every-download setting:** expose `acoustid_verify_every` (default off)?
   The per-file cost (~0.2–1.5 s + 1 lookup) is small but the benefit over the
   existing verifier is marginal on the happy path.
3. **Threshold calibration:** 0.8 confirm / 0.4 display are literature-typical
   starting points; a quick calibration run (fpcalc + lookup on a sample of the
   actual library, e.g. 20 tracks with known titles) would confirm them for
   regional content.
4. **ISRC availability** for regional tracks is sparse in AcoustID — when the
   candidate has no ISRC, the "Download this version" path degrades to
   metadata search (still works, less precise). Acceptable?
5. **Retry-loop use:** when the verifier rejects a file and AcoustID identifies
   it as a *different known* track, should fnack optionally search for that
   track instead? (Probably out of scope — the manual-match screen already
   handles "the user wants a different song".)
6. **Fingerprint submission:** AcoustID's regional coverage improves only when
   users *submit* fingerprints (needs a per-user key — the one piece of
   "authentication"). Out of scope for the zero-auth requirement, but worth a
   future optional "submit verified downloads" toggle.
7. **pyacoustid vs fpcalc subprocess:** recommend fpcalc + `requests` (matches
   the subprocess pattern, no new pip deps). Confirm before implementation; the
   two are functionally interchangeable.
8. **Multi-artist/featured recordings:** AcoustID returns the full artist credit
   list; cross-check only against the expected *primary* artist, matching the
   existing verifier's behavior.
9. **Identify action scope:** only for files with a `local_path` (downloaded or
   imported). Failed downloads delete the file, so there is nothing to
   fingerprint — confirm this matches expectations.
