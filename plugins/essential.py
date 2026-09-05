"""Authoritative definition of the plugins shipped in the fnack Docker image.

This module is the **single source of truth** for which official plugins are
*essential* — the set baked into the image and auto-installed on first boot.

Relationship between the three plugin sets:

- **Official** — every first-party plugin published in the fnack-plugins
  repository (`/home/tajanthind/fnack-plugins`). The vendored copy lives in
  this repo's `bundled_plugins/` directory (see
  `wayfinder/tickets/plugin-bundled-sync-of-truth.md`). A few official
  plugins are intentionally NOT vendored/bundled (un-shipped: fnack.lidarr,
  fnack.clean-navidrome-artists, fnack.fix-navidrome-splits) — they remain
  fully installable from the fnack-plugins Marketplace so they can be
  rebuilt/kept as plugins without core changes. fnack.subsonic was removed
  from the official catalog outright (no longer shipped or installable).
- **Essential** — the small subset of official plugins required for the
  out-of-box, first-run workflow (below). The Docker build prunes
  `bundled_plugins/` to exactly this set (see
  `scripts/select_essential_plugins.py`), so the image auto-installs only
  these. Nothing else ships in the image.
- **Optional** — every other official plugin. They remain fully installable
  from the fnack-plugins repository via Settings → Plugins (Marketplace), and
  community plugins remain supported the same way. Core has no code path that
  depends on any optional plugin being present.

Why these four are essential (determined from the actual first-run
requirements, not "install everything official"):

1. `fnack.deezer-batch` — `artist.search` / `artist.discography` /
   `artist.info`: adding an artist and syncing/importing their discography is
   the first thing a new user does; without a metadata provider the search UI
   has nothing to resolve.
2. `fnack.spotify` — `track.resolve`: turns a Spotify URL into a track with
   an ISRC (zero-auth). SpotiFLAC's `can_handle` gates on the resolved
   `track.spotify_url`, so this is required for the primary downloader to
   ever engage.
3. `fnack.spotiflac` — `download.track`: the primary lossless downloader
   (Tidal/Qobuz/Deezer/SoundCloud, zero-auth).
4. `fnack.ytdlp` — `download.track`: the fallback downloader
   (YouTube/YouTube Music/SoundCloud) that takes over when SpotiFLAC cannot
   handle a track.

Explicitly NOT essential: MusicBrainz/iTunes (enrichment + fallback),
AcoustID (fingerprinting is a silent no-op without an API key), Navidrome
(media-server scan is a convenience trigger), VPN, Lidarr, webhooks,
reverse-proxy auth, and the library-task maintenance plugins (maintenance
runs as a core subprocess, not via `library.task`). All remain installable.

This constant is consumed by `scripts/select_essential_plugins.py` (the
Docker build step). Keep the parity test
`tests/architecture/test_essential_plugins.py` green when it changes.
"""

ESSENTIAL_PLUGINS = frozenset({
    "fnack.spotiflac",     # primary lossless downloader (download.track)
    "fnack.ytdlp",         # fallback downloader (download.track)
    "fnack.spotify",       # track.resolve → Spotify URL for spotiflac
    "fnack.deezer-batch",  # artist.search / artist.discography / artist.info
})
