# Diagnose the 180 failing tracks — research findings

**Source**: `/home/tajanthind/fnack-info/fnack.db` + `fnack-logs.txt` (Aug 28–29).
**Status**: research (no code changed).

## Evidence

180 failed tracks. Failure clusters by error text:

| count | error |
|------:|-------|
| 119 | `Search returned a playlist/album, not a track: [soundcloud:search] ...` |
| 24  | verifier tag-mismatch / wrong-song rejections |
| 4   | `ERROR: [soundcloud] ... This video is DRM protected` |
| ~6  | SpotiFLAC 429 / track not found |
| ~27 | misc SoundCloud/YouTube errors |

Failures by artist (top): G-Eazy 46, Sharry Maan 29, Bilal Saeed 22, Justin
Bieber 21, Happy Raikoti 19, Kahlon 13, HARNOOR 13, Sharry Mann 11, The Kid
Laroi 4, AJ Mitchell 2.

Key observations:

1. **66% of failures are the SoundCloud fallback producing junk** — `scsearch2:`
   returns playlists/compilations and DjPunjab-style rips (`Jail - DjPunjab.Com.Se`,
   `Mix by audio-joiner.com`), the verifier rejects them, and the last error
   recorded is the SoundCloud "playlist" message. This hits **G-Eazy and Justin
   Bieber as much as regional artists** — it is not a regional-music problem,
   it is the fallback being wrong for everything.
2. ~316 `scsearch` invocations in the log; each rejected candidate burns
   download-queue time (multiple seconds per attempt) before failing.
3. The machine currently **has working network + cookies** (no "Sign in to
   confirm you're not a bot" in this window; `cookies.txt` present, 14
   cookies) — the failures are candidate *quality*, not connectivity.
4. The verifier is doing exactly its job (zero mismatched songs kept): the 24
   "tag mismatch" failures are correctly-rejected wrong songs.

## Fixes evaluated

- **A. Drop / gate the SoundCloud search fallback** (high impact, low effort).
  It never succeeds for this library; removing it makes failed tracks fail
  *fast and cleanly* instead of after wasted searches. Recommendation: remove
  the two `scsearch2:` targets from `ytdlp_service.download_track_ytdlp`
  (keep direct SoundCloud URL downloads — those still work when a real
  SoundCloud link is provided). Net effect: 119 tracks fail immediately with
  the *real* reason (previous source failed), queue time per failure drops
  minutes.
- **B. Fresh YouTube cookies + residential VPN** (high impact for the
  remaining causes). YouTube bot-checks and zarz.moe 429s are IP/cookie bound.
  The split-mode VPN (v0.2.31+) already routes only downloads through the
  tunnel; once the WireGuard peer actually handshakes, the 429s disappear and
  YouTube candidates improve. Cookie refresh is a manual, one-time user action.
- **C. AcoustID verification + identification** (medium impact, optional):
  catches "right file, wrong tags" (the 24 tag-mismatch group — some are the
  correct song with wrong embedded metadata), and the manual "Identify this
  file" flow rescues unknown regional tracks. Design landed separately in
  `acoustid-fingerprinting.md`.
- **D. MusicBrainz candidate enrichment** (later, optional): better search
  candidates for major artists (YouTube Topic channels resolved via
  MusicBrainz recordings). Design in `musicbrainz-integration.md`.

## Recommendation (top 3)

1. **Remove the SoundCloud search fallback** (fix A) — immediate, safe, kills
   2/3 of the failure noise and wasted queue time.
2. **Ship the optional AcoustID "verify when unsure / identify this file"
   flow** (fix C) — converts mislabeled-but-correct files into successes and
   gives regional tracks a real identification path.
3. **Get the VPN handshake working + refresh cookies** (fix B) — the
   environmental lever for bot-checks and 429s; split-mode already isolates
   the dashboard from a dead tunnel.

Expected outcome: the 119 SoundCloud-junk failures become fast, honest
failures; a meaningful share of the remaining 61 become recoverable via
AcoustID confirmation / identification; the rest wait on the VPN/cookies.
