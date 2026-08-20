"""Interactive Root Folder Importer Service: Discovers local artist folders, matches with Deezer discographies, and maps local files."""

import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Optional
import mutagen

from models import Album, Artist, Track, db
from services.deezer_service import get_artist_discography, search_artist

logger = logging.getLogger("fnack.import")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _clean_title(t: str) -> str:
    if not t:
        return ""
    # Strip leading track numbers like "01. ", "01 - ", "1 "
    t = re.sub(r"^\d+[\s.\-_]+", "", t)
    # Strip common suffixes like (feat. ...), [Official Video], [Remix], etc.
    t = re.sub(r"\s*\([^)]*(feat|ft|official|audio|remix|video)[^)]*\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\[[^\]]*(feat|ft|official|audio|remix|video)[^\]]*\]", "", t, flags=re.IGNORECASE)
    return t.strip()


def scan_root_folder_candidates(music_path: str) -> list[dict]:
    """
    Scan root music folder and return list of artist folder candidates for interactive import.
    Uses multi-factor scoring (folder name, albumartist tags, track tags) to accurately identify artists.
    """
    root = Path(music_path)
    if not root.is_dir():
        return []

    candidates = []
    existing_artists = Artist.query.all()
    existing_by_name = {_normalize(a.name): a.id for a in existing_artists}
    existing_by_id = {a.spotify_id: a.id for a in existing_artists if a.spotify_id}

    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue

        audio_files = [f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
        if not audio_files:
            continue

        # Sample audio files across the folder to extract tags
        albumartists = Counter()
        artists = Counter()
        albums_set = set()

        sample_files = (
            audio_files[:40]
            if len(audio_files) <= 40
            else [audio_files[i] for i in range(0, len(audio_files), max(1, len(audio_files) // 40))][:40]
        )

        for f in sample_files:
            try:
                mf = mutagen.File(str(f), easy=True)
                if mf:
                    tags = dict(mf) if hasattr(mf, "items") else {}
                    for art in tags.get("albumartist", []):
                        if art and str(art).strip():
                            albumartists[str(art).strip()] += 2
                    for art in tags.get("artist", []):
                        if art and str(art).strip():
                            artists[str(art).strip()] += 1
                    for alb in tags.get("album", []):
                        if alb and str(alb).strip():
                            albums_set.add(str(alb).strip())
            except Exception:
                pass

        if not albums_set:
            albums_set = {p.parent.name for p in audio_files if p.parent != folder}

        top_albumartist = albumartists.most_common(1)[0][0] if albumartists else None
        top_artist = artists.most_common(1)[0][0] if artists else None

        # Best detected artist name from tags
        detected_name = top_albumartist or top_artist or folder.name

        # Check if already imported
        already_id = (
            existing_by_name.get(_normalize(folder.name))
            or existing_by_name.get(_normalize(detected_name))
            or (top_artist and existing_by_name.get(_normalize(top_artist)))
        )

        # Search Deezer suggestions if not imported
        suggested = None
        alternate_matches = []
        if not already_id:
            # Search Deezer using folder name first
            results = search_artist(folder.name, limit=6)

            # If folder name returned few results or if detected tag is different, also search with detected tag
            if top_artist and _normalize(top_artist) != _normalize(folder.name) and len(results) < 3:
                more_results = search_artist(top_artist, limit=4)
                existing_res_ids = {r["id"] for r in results}
                for r in more_results:
                    if r["id"] not in existing_res_ids:
                        results.append(r)

            folder_norm = _normalize(folder.name)
            tag_norm = _normalize(detected_name)

            def _score_candidate(c):
                c_norm = _normalize(c.get("name", ""))
                score = 0
                if c_norm == folder_norm:
                    score += 1000  # Exact match to folder name
                elif folder_norm and len(folder_norm) > 2 and (c_norm.startswith(folder_norm) or folder_norm in c_norm):
                    score += 500
                elif c_norm == tag_norm:
                    score += 400  # Exact match to top metadata tag
                elif tag_norm and len(tag_norm) > 2 and (c_norm.startswith(tag_norm) or tag_norm in c_norm):
                    score += 200
                score += min(50, c.get("nb_album", 0))
                return score

            if results:
                results.sort(key=_score_candidate, reverse=True)
                suggested = results[0]
                alternate_matches = results[:5]

        candidates.append({
            "folder_name": folder.name,
            "detected_artist": detected_name,
            "track_count": len(audio_files),
            "album_count": len(albums_set) or 1,
            "is_already_imported": bool(already_id),
            "existing_artist_id": already_id,
            "suggested_deezer": suggested,
            "alternate_matches": alternate_matches,
        })

    return candidates


def import_artist_folder(
    music_path: str,
    folder_name: str,
    deezer_artist_id: Optional[int] = None,
    filter_options: Optional[dict] = None,
) -> dict:
    """
    Import an artist folder: Pulls Deezer discography and maps existing local audio files.
    """
    root = Path(music_path)
    artist_dir = root / folder_name
    if not artist_dir.is_dir():
        return {"error": f"Folder '{folder_name}' not found in {music_path}"}

    audio_files = [f for f in artist_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
    opts = filter_options or {}

    # If deezer_artist_id not provided, try to search
    if not deezer_artist_id:
        results = search_artist(folder_name, limit=1)
        if results:
            deezer_artist_id = results[0]["id"]

    if not deezer_artist_id:
        return {"error": f"Could not resolve Deezer artist for folder '{folder_name}'"}

    # Fetch discography
    disco = get_artist_discography(
        deezer_artist_id,
        filter_remixes=opts.get("filter_remixes", True),
        filter_lofi=opts.get("filter_lofi", True),
        filter_live=opts.get("filter_live", True),
        filter_compilations=opts.get("filter_compilations", True),
        include_albums=opts.get("include_albums", True),
        include_singles=opts.get("include_singles", True),
        include_compilations=opts.get("include_compilations", False),
    )

    artist_name = disco["artist_name"]
    artist = Artist.query.filter_by(spotify_id=str(deezer_artist_id)).first()
    if not artist:
        # Check by exact name match before creating
        artist = Artist.query.filter(db.func.lower(Artist.name) == artist_name.lower()).first()
        if artist:
            artist.spotify_id = str(deezer_artist_id)
            artist.image_url = disco["artist_image"]
        else:
            artist = Artist(
                spotify_id=str(deezer_artist_id),
                name=artist_name,
                image_url=disco["artist_image"],
                source="folder",
                monitored=opts.get("monitored", True),
                auto_download=opts.get("auto_download", False),
                filter_remixes=opts.get("filter_remixes", True),
                filter_lofi=opts.get("filter_lofi", True),
                filter_live=opts.get("filter_live", True),
                filter_compilations=opts.get("filter_compilations", True),
                include_albums=opts.get("include_albums", True),
                include_singles=opts.get("include_singles", True),
                include_compilations=opts.get("include_compilations", False),
            )
            db.session.add(artist)
            db.session.flush()

    # Index discography albums & tracks into lookup maps
    track_lookup: dict[str, Track] = {}
    isrc_lookup: dict[str, Track] = {}

    for a in disco.get("albums", []):
        album = Album.query.filter_by(artist_id=artist.id, deezer_id=str(a["id"])).first()
        if not album:
            album = Album(
                artist_id=artist.id,
                name=a["title"],
                year=a.get("year"),
                cover_url=a.get("cover_url"),
                deezer_id=str(a["id"]),
                record_type=a.get("record_type", "album"),
            )
            db.session.add(album)
            db.session.flush()

        for t in a.get("tracks", []):
            track = Track.query.filter_by(album_id=album.id, deezer_id=str(t["id"])).first()
            if not track:
                track = Track(
                    album_id=album.id,
                    artist_id=artist.id,
                    title=t["title"],
                    track_number=t.get("track_position"),
                    disc_number=t.get("disk_number", 1),
                    duration=t.get("duration"),
                    isrc=t.get("isrc"),
                    deezer_id=str(t["id"]),
                    status="missing",
                )
                db.session.add(track)
                db.session.flush()

            # Index by ISRC
            if track.isrc:
                isrc_lookup[track.isrc.strip().upper()] = track

            # Add to matching lookup keys: (norm_album, norm_title) & norm_title (both raw and cleaned)
            norm_alb = _normalize(album.name)
            norm_alb_clean = _normalize(_clean_title(album.name))
            norm_tit = _normalize(track.title)
            norm_tit_clean = _normalize(_clean_title(track.title))

            if norm_alb and norm_tit:
                track_lookup[f"{norm_alb}::{norm_tit}"] = track
            if norm_alb and norm_tit_clean:
                track_lookup[f"{norm_alb}::{norm_tit_clean}"] = track
            if norm_alb_clean and norm_tit:
                track_lookup[f"{norm_alb_clean}::{norm_tit}"] = track
            if norm_alb_clean and norm_tit_clean:
                track_lookup[f"{norm_alb_clean}::{norm_tit_clean}"] = track

            if norm_tit and norm_tit not in track_lookup:
                track_lookup[norm_tit] = track
            if norm_tit_clean and norm_tit_clean not in track_lookup:
                track_lookup[norm_tit_clean] = track

    db.session.commit()

    # Match local files against discography
    matched_count = 0
    unmatched_files = []

    for fp in audio_files:
        meta_album, meta_title, meta_dur, meta_isrc = None, None, None, None
        try:
            mf = mutagen.File(str(fp), easy=True)
            if mf:
                tags = dict(mf) if hasattr(mf, "items") else {}
                meta_album = tags.get("album", [None])[0]
                meta_title = tags.get("title", [None])[0]
                meta_isrc = tags.get("isrc", [None])[0]
            if mf and mf.info:
                meta_dur = getattr(mf.info, "length", None)
        except Exception:
            pass

        # 1. Try matching by ISRC
        target_track = None
        if meta_isrc and str(meta_isrc).strip().upper() in isrc_lookup:
            target_track = isrc_lookup[str(meta_isrc).strip().upper()]

        # 2. Try matching by Album + Title, then Title alone
        if not target_track:
            cand_titles = [
                _normalize(meta_title),
                _normalize(_clean_title(meta_title)),
                _normalize(fp.stem),
                _normalize(_clean_title(fp.stem)),
            ]
            cand_albs = [
                _normalize(meta_album),
                _normalize(_clean_title(meta_album)),
                _normalize(fp.parent.name),
                _normalize(_clean_title(fp.parent.name)),
            ]

            for ca in cand_albs:
                for ct in cand_titles:
                    if ca and ct and f"{ca}::{ct}" in track_lookup:
                        target_track = track_lookup[f"{ca}::{ct}"]
                        break
                if target_track:
                    break

            if not target_track:
                for ct in cand_titles:
                    if ct and ct in track_lookup:
                        target_track = track_lookup[ct]
                        break

        # If matched, map local file
        if target_track and not target_track.is_downloaded:
            rel = str(fp.relative_to(root))
            target_track.is_downloaded = True
            target_track.status = "completed"
            target_track.local_path = str(fp)
            target_track.file_path = rel
            target_track.file_format = fp.suffix.lower().lstrip(".")
            target_track.size_bytes = fp.stat().st_size
            if meta_dur:
                target_track.duration = meta_dur
            matched_count += 1
        elif target_track and target_track.is_downloaded:
            # If already marked downloaded, verify if local file path is valid or update
            if not target_track.local_path or not Path(target_track.local_path).exists():
                rel = str(fp.relative_to(root))
                target_track.local_path = str(fp)
                target_track.file_path = rel
                target_track.file_format = fp.suffix.lower().lstrip(".")
                target_track.size_bytes = fp.stat().st_size
                if meta_dur:
                    target_track.duration = meta_dur
            matched_count += 1
        else:
            title_cand = meta_title or _clean_title(fp.stem)
            alb_cand = meta_album or fp.parent.name
            unmatched_files.append((fp, title_cand, alb_cand, meta_dur))

    # Add unmatched local tracks under a special "Unmatched Local Tracks" album
    if unmatched_files:
        unmatched_album = Album.query.filter_by(artist_id=artist.id, name="Unmatched Local Tracks").first()
        if not unmatched_album:
            unmatched_album = Album(
                artist_id=artist.id,
                name="Unmatched Local Tracks",
                record_type="other",
                is_downloaded=True,
            )
            db.session.add(unmatched_album)
            db.session.flush()

        for fp, title, alb_name, dur in unmatched_files:
            rel = str(fp.relative_to(root))
            existing_unmatched = Track.query.filter_by(album_id=unmatched_album.id, file_path=rel).first()
            if not existing_unmatched:
                db.session.add(Track(
                    album_id=unmatched_album.id,
                    artist_id=artist.id,
                    title=f"{title} [{alb_name}]",
                    file_path=rel,
                    local_path=str(fp),
                    file_format=fp.suffix.lower().lstrip("."),
                    duration=dur,
                    size_bytes=fp.stat().st_size,
                    is_downloaded=True,
                    status="completed",
                    is_unmatched=True,
                ))

    # Update all album statuses
    for album in artist.albums.all():
        tracks = album.tracks.all()
        if tracks:
            album.is_downloaded = all(t.is_downloaded for t in tracks)
            album.size_bytes = sum(t.size_bytes or 0 for t in tracks)
            first_downloaded = next((t for t in tracks if t.local_path), None)
            if first_downloaded and first_downloaded.local_path:
                album.local_path = str(Path(first_downloaded.local_path).parent)

    db.session.commit()
    logger.info("[IMPORT] Imported artist '%s': %d local files matched, %d unmatched", artist_name, matched_count, len(unmatched_files))

    return {
        "artist_id": artist.id,
        "artist_name": artist_name,
        "matched_tracks": matched_count,
        "unmatched_tracks": len(unmatched_files),
        "total_local_files": len(audio_files),
    }
