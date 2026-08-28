"""Metadata normalization service.

Keeps the music library's tags aligned with the fnack database so Navidrome
groups albums correctly (no duplicate/split albums from mismatched ALBUM or
ALBUMARTIST tags left over by older versions or imports).

Runs automatically at container startup and periodically (via the scheduler);
can also be invoked manually. Files whose tags already match the expected
metadata are skipped, so steady-state runs are fast.

Also merges duplicate database album rows (same artist, album name equal
ignoring case/whitespace, and one album's track titles a subset of the
other's) — Deezer exposes many releases under two IDs (album + single with
identical content), which used to split albums inside Navidrome forever.
"""

import logging
import os
import re
import shutil
import unicodedata
from collections import defaultdict
from pathlib import Path

from models import Track, db
from services.queue_service import _sanitize, _tag_audio_file

logger = logging.getLogger("fnack.metadata")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
MUSIC_ROOT = Path(os.environ.get("MUSIC_DIR", "/music"))

_TYPE_RANK = {"album": 3, "ep": 2, "single": 1}

# Edition suffixes that are the same release, not a different album
# ("Nine Track Mind" == "Nine Track Mind (Deluxe Edition)").
_EDITION_SUFFIX_RE = re.compile(
    r"(\s*[\(\[]\s*(deluxe edition|super deluxe edition|super deluxe|deluxe version|deluxe|special edition|expanded edition|expanded|bonus track version|bonus tracks|limited edition|anniversary edition|remastered edition|remaster|explicit version|clean version|international version|uk version|japanese edition|target edition|itunes version|spotify version)[^)\]]*[\)\]])+$",
    re.IGNORECASE,
)

_LOSSLESS_RANK = {".flac": 5, ".wav": 5, ".alac": 4, ".m4a": 4, ".opus": 2, ".ogg": 2, ".mp3": 1, ".aac": 1}


def _norm(s) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _norm_album_name(s) -> str:
    """Normalize an album name for duplicate comparison (ignores edition suffixes)."""
    return _norm(_EDITION_SUFFIX_RE.sub("", s or ""))


def _compatible(set_a, set_b) -> bool:
    """True when two track-title sets are the same content: one contains the
    other, or they overlap heavily (>= 75% of the smaller set). This merges
    identical albums, standard-vs-deluxe editions and fuzzy-spelled copies,
    while leaving genuinely different releases (e.g. 50% overlap) alone."""
    if not set_a or not set_b:
        return False
    inter = len(set_a & set_b)
    return inter / min(len(set_a), len(set_b)) >= 0.75


def _file_rank(path) -> int:
    return _LOSSLESS_RANK.get(Path(path).suffix.lower(), 0)


def _read_simple_tag(mf, keys):
    for k in keys:
        v = mf.get(k)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v:
            return str(v)
    return None


def _source_rank(al) -> int:
    """Prefer real Deezer albums over iTunes fallback rows (often misspelled)."""
    dz = str(al.deezer_id or "")
    return 1 if dz and not dz.startswith("itunes_") else 0


def _pick_canonical(a, b, set_a, set_b, dl_a, dl_b):
    """Choose which of two same-content albums should survive the merge."""
    if len(set_a) != len(set_b):
        return a if len(set_a) > len(set_b) else b
    if dl_a != dl_b:
        return a if dl_a > dl_b else b
    if _source_rank(a) != _source_rank(b):
        return a if _source_rank(a) > _source_rank(b) else b
    ra, rb = _TYPE_RANK.get(a.record_type or "", 0), _TYPE_RANK.get(b.record_type or "", 0)
    if ra != rb:
        return a if ra > rb else b
    return a if a.id < b.id else b


def _merge_album_into(canonical, other, canonical_map, stats, cross_artist: bool = False) -> None:
    """Fold every track of `other` into `canonical` (dup titles are dropped,
    along with their duplicate files), then delete the `other` album row."""
    from models import DownloadJob

    for track in list(other.tracks.all()):
        nt = _norm(track.title)
        existing = canonical_map.get(nt) if nt else None
        if existing is not None:
            if track.is_downloaded and not existing.is_downloaded:
                # The canonical copy is missing but this one is downloaded —
                # adopt its file reference before dropping the dup row.
                existing.local_path = track.local_path
                existing.file_path = track.file_path
                existing.file_format = track.file_format
                existing.duration = track.duration
                existing.size_bytes = track.size_bytes
                existing.is_downloaded = True
                existing.status = "completed"
            elif (
                track.is_downloaded and existing.is_downloaded
                and track.local_path and existing.local_path
                and track.local_path != existing.local_path
            ):
                # Same song downloaded twice (album imported under two
                # spellings/editions/artists). Keep the higher-quality file and
                # delete the other copy (also breaks hardlinks between the two
                # album folders) so Navidrome stops showing a duplicate album
                # from the orphaned files.
                if _file_rank(track.local_path) > _file_rank(existing.local_path):
                    try:
                        if os.path.isfile(existing.local_path):
                            os.remove(existing.local_path)
                    except OSError:
                        pass
                    existing.local_path = track.local_path
                    existing.file_path = track.file_path
                    existing.file_format = track.file_format
                    existing.duration = track.duration
                    existing.size_bytes = track.size_bytes
                else:
                    try:
                        if os.path.isfile(track.local_path):
                            os.remove(track.local_path)
                    except OSError:
                        pass
            DownloadJob.query.filter_by(track_id=track.id).delete()
            db.session.delete(track)
            stats["removed_dup_tracks"] += 1
        else:
            track.album_id = canonical.id
            if cross_artist and track.artist_id != canonical.artist_id:
                track.artist_id = canonical.artist_id
            if nt:
                canonical_map[nt] = track
            stats["merged_tracks"] += 1

    if not canonical.year and other.year:
        canonical.year = other.year
    if not canonical.cover_url and other.cover_url:
        canonical.cover_url = other.cover_url
    db.session.delete(other)
    stats["merged_albums"] += 1


def _merge_duplicate_albums(app) -> dict:
    """Merge duplicate album rows so Navidrome stops splitting one release
    across multiple albums.

    Two passes, both restricted to same-artist albums:
      1. exact:  album names equal ignoring case + whitespace, and
      2. fuzzy:  names similar (e.g. "Patander" vs "Patandar" — Deezer and
                 the iTunes fallback frequently spell the same release
                 differently).
    A merge only happens when one album's normalized track titles are a
    subset of the other's, so real single-vs-album pairs with different
    tracks are never combined.

    Returns a stats dict: {merged_albums, merged_tracks, removed_dup_tracks}.
    """
    from difflib import SequenceMatcher

    import sqlalchemy as sa
    from models import Album

    stats = {"merged_albums": 0, "merged_tracks": 0, "removed_dup_tracks": 0}

    def _try_merge(albums: list):
        """Attempt to merge a list of same-name/similar albums into one."""
        if len(albums) < 2:
            return
        if any((a.name or "").lower() == "unmatched local tracks" for a in albums):
            return
        sets = {al.id: {_norm(t.title) for t in al.tracks.all() if t.title} for al in albums}
        dl = {al.id: sum(1 for t in al.tracks.all() if t.is_downloaded) for al in albums}

        canonical = albums[0]
        for al in albums[1:]:
            canonical = _pick_canonical(canonical, al, sets[canonical.id], sets[al.id], dl[canonical.id], dl[al.id])

        others = [al for al in albums if al.id != canonical.id]
        if any(not _compatible(sets[o.id], sets[canonical.id]) for o in others):
            logger.info(
                "[METADATA] Skipping merge for '%s' (%d albums): track lists differ",
                canonical.name, len(albums),
            )
            return

        canonical_map = {_norm(t.title): t for t in canonical.tracks.all() if t.title}
        for other in others:
            _merge_album_into(canonical, other, canonical_map, stats)

        canon_tracks = canonical.tracks.all()
        canonical.is_downloaded = bool(canon_tracks) and all(t.is_downloaded for t in canon_tracks)
        canonical.size_bytes = sum(t.size_bytes or 0 for t in canon_tracks)
        db.session.commit()
        logger.info(
            "[METADATA] Merged duplicate album '%s' (id %d): %d album(s), "
            "%d track(s) moved, %d duplicate track(s) removed",
            canonical.name, canonical.id, stats["merged_albums"], stats["merged_tracks"], stats["removed_dup_tracks"],
        )

    with app.app_context():
        # ---- Pass 1: exact name matches (case/whitespace-insensitive) ----
        norm_col = sa.func.lower(sa.func.trim(Album.name))
        groups = (
            db.session.query(Album.artist_id, norm_col.label("norm_name"))
            .group_by(Album.artist_id, norm_col)
            .having(sa.func.count(sa.distinct(Album.id)) > 1)
            .all()
        )
        for artist_id, norm_name in groups:
            if not norm_name:
                continue
            albums = (
                Album.query.filter_by(artist_id=artist_id)
                .filter(norm_col == norm_name)
                .all()
            )
            _try_merge(albums)

        # ---- Pass 2: fuzzy name variants within each artist ----
        # Deezer + iTunes fallback often disagree on spelling ("Patander" vs
        # "Patandar", "Gutt" vs "Gut"); catch those when the track lists match.
        artist_ids = [r[0] for r in db.session.query(Album.artist_id).distinct().all()]
        for artist_id in artist_ids:
            folded = set()
            while True:
                albums = [
                    al for al in Album.query.filter_by(artist_id=artist_id).all()
                    if al.id not in folded and (al.name or "").lower() != "unmatched local tracks"
                ]
                if len(albums) < 2:
                    break
                sets = {al.id: {_norm(t.title) for t in al.tracks.all() if t.title} for al in albums}
                dl = {al.id: sum(1 for t in al.tracks.all() if t.is_downloaded) for al in albums}
                norm_names = {al.id: _norm(al.name) for al in albums}

                merged_something = False
                for i in range(len(albums)):
                    for j in range(i + 1, len(albums)):
                        a, b = albums[i], albums[j]
                        na, nb = norm_names[a.id], norm_names[b.id]
                        if not na or not nb or len(na) < 4 or len(nb) < 4:
                            continue
                        same_name = (
                            _norm_album_name(a.name) == _norm_album_name(b.name)
                            or SequenceMatcher(None, na, nb).ratio() >= 0.85
                        )
                        if not same_name:
                            continue
                        if not _compatible(sets[a.id], sets[b.id]):
                            continue
                        canonical = _pick_canonical(a, b, sets[a.id], sets[b.id], dl[a.id], dl[b.id])
                        other = b if canonical.id == a.id else a
                        canonical_map = {_norm(t.title): t for t in canonical.tracks.all() if t.title}
                        _merge_album_into(canonical, other, canonical_map, stats)
                        canon_tracks = canonical.tracks.all()
                        canonical.is_downloaded = bool(canon_tracks) and all(t.is_downloaded for t in canon_tracks)
                        canonical.size_bytes = sum(t.size_bytes or 0 for t in canon_tracks)
                        db.session.commit()
                        folded.add(other.id)
                        merged_something = True
                        logger.info(
                            "[METADATA] Merged fuzzy duplicate '%s' -> '%s' (id %d): %d album(s), "
                            "%d track(s) moved, %d duplicate track(s) removed",
                            other.name, canonical.name, canonical.id,
                            stats["merged_albums"], stats["merged_tracks"], stats["removed_dup_tracks"],
                        )
                        break
                    if merged_something:
                        break
                if not merged_something:
                    break

        # ---- Pass 3: cross-artist duplicates ----
        # Collab releases (e.g. Cheema Y & Gur Sidhu) get imported once per
        # member artist, and the same song file is often hardlinked in both
        # album folders — which made the per-file retagger flip-flop the tags
        # (last write wins for both paths) and Navidrome split the album per
        # artist. Merge same-name, same-content albums across artists into one.
        # Grouped by normalized name so the pair scan stays small.
        while True:
            groups = defaultdict(list)
            for al in Album.query.all():
                if (al.name or "").lower() == "unmatched local tracks":
                    continue
                groups[_norm_album_name(al.name)].append(al)

            merged_something = False
            for albs in groups.values():
                if len(albs) < 2:
                    continue
                sets = {al.id: {_norm(t.title) for t in al.tracks.all() if t.title} for al in albs}
                dl = {al.id: sum(1 for t in al.tracks.all() if t.is_downloaded) for al in albs}
                norm_names = {al.id: _norm(al.name) for al in albs}

                for i in range(len(albs)):
                    for j in range(i + 1, len(albs)):
                        a, b = albs[i], albs[j]
                        if a.artist_id == b.artist_id:
                            continue
                        na, nb = norm_names[a.id], norm_names[b.id]
                        if not na or not nb or len(na) < 4 or len(nb) < 4:
                            continue
                        same_name = (
                            _norm_album_name(a.name) == _norm_album_name(b.name)
                            or SequenceMatcher(None, na, nb).ratio() >= 0.85
                        )
                        if not same_name:
                            continue
                        # Guard against real same-name releases by different artists
                        if min(len(sets[a.id]), len(sets[b.id])) < 2:
                            continue
                        if not _compatible(sets[a.id], sets[b.id]):
                            continue
                        canonical = _pick_canonical(a, b, sets[a.id], sets[b.id], dl[a.id], dl[b.id])
                        other = b if canonical.id == a.id else a
                        # capture names before the merge deletes rows / commits
                        other_artist_name = other.artist.name if other.artist else "?"
                        canonical_artist_name = canonical.artist.name if canonical.artist else "?"
                        canonical_map = {_norm(t.title): t for t in canonical.tracks.all() if t.title}
                        _merge_album_into(canonical, other, canonical_map, stats, cross_artist=True)
                        canon_tracks = canonical.tracks.all()
                        canonical.is_downloaded = bool(canon_tracks) and all(t.is_downloaded for t in canon_tracks)
                        canonical.size_bytes = sum(t.size_bytes or 0 for t in canon_tracks)
                        db.session.commit()
                        merged_something = True
                        logger.info(
                            "[METADATA] Merged cross-artist duplicate '%s' (%s) -> '%s' (%s, id %d): "
                            "%d track(s) moved, %d duplicate track(s) removed",
                            other.name, other_artist_name,
                            canonical.name, canonical_artist_name, canonical.id,
                            stats["merged_tracks"], stats["removed_dup_tracks"],
                        )
                        break
                    if merged_something:
                        break
                if merged_something:
                    break
            if not merged_something:
                break

    return stats


def _remove_empty_album_dirs() -> None:
    """Delete leftover empty album folders (e.g. after duplicate-file cleanup),
    including any stray cover.jpg/folder.jpg they contain."""
    try:
        if not MUSIC_ROOT.is_dir():
            return
        for artist_dir in MUSIC_ROOT.iterdir():
            if not artist_dir.is_dir():
                continue
            for album_dir in artist_dir.iterdir():
                if not album_dir.is_dir():
                    continue
                for cover in ("cover.jpg", "folder.jpg"):
                    cover_path = album_dir / cover
                    if cover_path.is_file():
                        try:
                            cover_path.unlink()
                        except OSError:
                            pass
                try:
                    album_dir.rmdir()
                except OSError:
                    pass  # not empty — keep
    except OSError:
        pass


def _backfill_album_artwork(app) -> int:
    """Ensure every downloaded album has cover art files on disk.

    Albums mapped from an existing library (folder import) never go through
    the download path, so their cover.jpg was never written even though the
    DB has cover_url. Navidrome then shows those albums without artwork.
    This fetches and saves any missing covers (parallelized, skips existing).
    Returns the number of covers saved.
    """
    from models import Album, AppSetting, Track

    with app.app_context():
        s_save = db.session.get(AppSetting, "save_cover_art")
        save_cover = s_save.value.lower() != "false" if s_save else True
        s_fn = db.session.get(AppSetting, "cover_art_filename")
        cover_filename = s_fn.value.strip() if s_fn and s_fn.value.strip() else "cover.jpg"
        if not save_cover:
            return 0

        from services.queue_service import _save_album_cover

        # Only albums that actually have downloaded files are candidates; only
        # those whose folder is missing a cover get fetched. Two queries total.
        tracks = (
            Track.query.filter(Track.is_downloaded == True, Track.local_path.isnot(None))  # noqa: E712
            .with_entities(Track.album_id, Track.local_path)
            .all()
        )
        album_folder: dict = {}
        for album_id, local_path in tracks:
            if album_id not in album_folder:
                album_folder[album_id] = Path(local_path).parent

        rows = db.session.query(Album.id, Album.cover_url).filter(Album.cover_url.isnot(None)).all()

        jobs = []  # (dest_dir, cover_url)
        seen_dirs = set()
        for album_id, cover_url in rows:
            if not cover_url:
                continue
            dest_dir = album_folder.get(album_id)
            if dest_dir is None or not dest_dir.is_dir():
                continue
            resolved = dest_dir.resolve()
            if resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)
            has_cover = any(
                (dest_dir / fn).is_file() and (dest_dir / fn).stat().st_size > 1024
                for fn in ("cover.jpg", "folder.jpg")
            )
            if not has_cover:
                jobs.append((dest_dir, cover_url))

        saved = 0
        if not jobs:
            return 0

        def _fetch(job):
            dest_dir, url = job
            before = set(dest_dir.iterdir()) if dest_dir.is_dir() else set()
            _save_album_cover(dest_dir, url, save_cover=True, cover_filename=cover_filename)
            after = set(dest_dir.iterdir()) if dest_dir.is_dir() else set()
            return 1 if after - before else 0

        try:
            import gevent
            from gevent.pool import Pool
            pool = Pool(min(8, len(jobs)))
            results = pool.map(_fetch, jobs)
            saved = sum(r for r in results if r)
        except Exception:
            for job in jobs:
                try:
                    _fetch(job)
                    saved += 1
                except Exception:
                    pass

    logger.info("[METADATA] Artwork backfill: %d cover(s) saved", saved)
    return saved


def normalize_album_tags(app, quiet: bool = True) -> dict:
    """Re-tag every downloaded file with its database album/artist/title and move
    stray files into their correct album folder. Returns stats.

    Skipped when a file's ALBUM + ALBUMARTIST already match the expected values,
    so repeated runs only touch files that actually need fixing.
    """
    import mutagen

    stats = {"checked": 0, "retagged": 0, "moved": 0, "skipped": 0, "errors": 0}

    # First collapse duplicate DB albums (Deezer exposes many releases twice).
    # Without this, files get tagged with two spellings of the same album and
    # Navidrome keeps showing them split no matter how many times it rescans.
    merge_stats = _merge_duplicate_albums(app)

    with app.app_context():
        tracks = Track.query.filter(Track.is_downloaded == True).all()  # noqa: E712
        for t in tracks:
            if not t.local_path or not os.path.isfile(t.local_path):
                stats["skipped"] += 1
                continue
            album = t.album
            if not album or not album.artist:
                stats["skipped"] += 1
                continue
            artist_name = album.artist.name
            album_name = album.name
            fp = Path(t.local_path)
            stats["checked"] += 1

            # ---- 1. Place the file in the folder its DB album belongs to ----
            expected_dir = MUSIC_ROOT / _sanitize(artist_name) / _sanitize(album_name)
            try:
                expected_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                stats["errors"] += 1
                logger.warning("[METADATA] Cannot create %s: %s", expected_dir, e)
                continue
            if fp.parent.resolve() != expected_dir.resolve():
                try:
                    new_path = expected_dir / fp.name
                    if new_path.exists() and new_path.resolve() != fp.resolve():
                        fp.unlink()
                        fp = new_path
                    else:
                        shutil.move(str(fp), str(new_path))
                        fp = new_path
                    t.local_path = str(fp)
                    t.file_path = str(fp.relative_to(MUSIC_ROOT))
                    stats["moved"] += 1
                    if not quiet:
                        logger.info("[METADATA] Moved %s -> %s", fp.name, fp)
                except OSError as e:
                    stats["errors"] += 1
                    logger.warning("[METADATA] Could not move %s: %s", fp, e)
                    continue

            # ---- 2. Re-tag only when something differs (fast steady state) ----
            try:
                mf = mutagen.File(str(fp))
                if mf is not None:
                    cur_album = _read_simple_tag(mf, ("album", "\xa9alb", "TALB"))
                    cur_albumartist = _read_simple_tag(mf, ("albumartist", "aART", "TPE2"))
                    # Per-track original/release dates (left by downloaders)
                    # make Navidrome split one album into many — any file still
                    # carrying them (or with a date different from the album
                    # year) must be re-tagged, which strips them.
                    has_stray_dates = any(
                        k.upper() in {"ORIGINALDATE", "RELEASEDATE", "TDOR", "TDRL"}
                        for k in getattr(mf, "keys", lambda: ())()
                    )
                    cur_date = _read_simple_tag(mf, ("date", "\xa9day", "TDRC"))
                    date_mismatch = bool(album.year) and bool(cur_date) and cur_date != str(album.year)
                    if (
                        cur_album == album_name
                        and cur_albumartist == artist_name
                        and not has_stray_dates
                        and not date_mismatch
                    ):
                        stats["skipped"] += 1
                        continue
            except Exception:
                pass

            cover_bytes = None
            for cover_name in ("cover.jpg", "folder.jpg"):
                cover_path = fp.parent / cover_name
                if cover_path.is_file():
                    try:
                        cover_bytes = cover_path.read_bytes()
                    except OSError:
                        pass
                    break

            try:
                _tag_audio_file(
                    fp,
                    artist=artist_name,
                    album=album_name,
                    title=t.title,
                    track_num=t.track_number or 0,
                    year=album.year,
                    album_artist=artist_name,
                    disc_num=t.disc_number or 1,
                    total_tracks=album.tracks.count(),
                    cover_bytes=cover_bytes,
                    genre=t.genre,
                )
                stats["retagged"] += 1
                if not quiet:
                    logger.info("[METADATA] Tagged %s - %s | %s", artist_name, album_name, t.title)
            except Exception as e:
                stats["errors"] += 1
                logger.warning("[METADATA] Tagging failed for %s: %s", fp, e)

        db.session.commit()

    # Drop empty album folders left behind by duplicate-file cleanup
    _remove_empty_album_dirs()

    # Ensure every downloaded album has cover art on disk (imported libraries
    # never saved covers). Navidrome shows albums without art otherwise.
    covers_backfilled = _backfill_album_artwork(app)

    logger.info(
        "[METADATA] Normalize pass: %d checked, %d retagged, %d moved, %d skipped, %d errors"
        " | duplicates: %d albums merged, %d tracks moved, %d dup tracks removed"
        " | artwork: %d covers saved",
        stats["checked"], stats["retagged"], stats["moved"], stats["skipped"], stats["errors"],
        merge_stats["merged_albums"], merge_stats["merged_tracks"], merge_stats["removed_dup_tracks"],
        covers_backfilled,
    )

    # If anything changed on disk or in tags, ask Navidrome to rescan so the
    # merged/retagged albums are regrouped (its scan is debounced server-side).
    if (
        stats["retagged"] > 0 or stats["moved"] > 0 or covers_backfilled > 0
        or merge_stats["merged_albums"] > 0 or merge_stats["merged_tracks"] > 0
    ):
        try:
            from services.navidrome_service import trigger_navidrome_scan
            trigger_navidrome_scan(app)
        except Exception:
            logger.debug("[METADATA] Could not trigger Navidrome scan after normalize", exc_info=True)

    stats.update(merge_stats)
    stats["covers_backfilled"] = covers_backfilled
    return stats
