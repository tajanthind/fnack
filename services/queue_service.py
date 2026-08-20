"""Download queue service: Thread-safe, non-blocking track-by-track execution with ISRC-first pipeline & verification."""

import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Set
import gevent
from flask import Flask
from flask_socketio import SocketIO

from models import Album, AppSetting, Artist, DownloadJob, Track, db
from services.deezer_service import get_track_info
from services.spotify_service import resolve_spotify_url
from services.spotiflac_service import download_track_spotiflac
from services.ytdlp_service import download_track_ytdlp
from services.verifier_service import STRICTNESS_DELTAS, verify_audio_file
from services.navidrome_service import trigger_navidrome_scan
import requests

logger = logging.getLogger("fnack.queue")

DOWNLOADS_DIR = Path("/downloads")
MUSIC_DIR = Path("/music")
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}

cancel_requested_jobs: Set[int] = set()
_executor: Optional[ThreadPoolExecutor] = None
_running_futures: set = set()
_queue_lock = threading.Lock()


def _sanitize(name: str) -> str:
    if not name:
        return "Unknown"
    for char in (":", "/", "\\", "?", "*", '"', "<", ">", "|"):
        name = name.replace(char, " -" if char == ":" else "-" if char in ("/", "\\", "|") else "")
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(".")


def _save_album_cover(
    dest_dir: Path,
    cover_url: Optional[str],
    save_cover: bool = True,
    cover_filename: str = "cover.jpg",
) -> Optional[bytes]:
    """Download and save album cover art in the album folder as cover.jpg / folder.jpg."""
    if not cover_url or not save_cover:
        return None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        filenames = ["cover.jpg"]
        if "folder" in cover_filename.lower():
            if "both" in cover_filename.lower():
                filenames = ["cover.jpg", "folder.jpg"]
            else:
                filenames = ["folder.jpg"]

        # Check existing valid cover art
        for fn in filenames:
            fp = dest_dir / fn
            if fp.exists() and fp.stat().st_size > 1024:
                try:
                    return fp.read_bytes()
                except OSError:
                    pass

        # Download from URL
        resp = requests.get(cover_url, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 1024:
            img_bytes = resp.content
            for fn in filenames:
                (dest_dir / fn).write_bytes(img_bytes)
            logger.info("[QUEUE] Saved album artwork (%d bytes) to %s", len(img_bytes), dest_dir)
            return img_bytes
    except Exception as e:
        logger.debug("[QUEUE] Cover art download note for %s: %s", dest_dir, e)
    return None


def _tag_audio_file(
    file_path: Path,
    artist: str,
    album: str,
    title: str,
    track_num: int = 0,
    year: Optional[int] = None,
    album_artist: Optional[str] = None,
    disc_num: int = 1,
    total_tracks: Optional[int] = None,
    cover_bytes: Optional[bytes] = None,
) -> None:
    """
    Embed clean, uniform metadata tags (artist, album artist, album, title, track number, disc, year)
    and front cover artwork across all formats using Mutagen, eliminating casing conflicts for Navidrome.
    """
    try:
        ext = file_path.suffix.lower()
        effective_album_artist = (album_artist or artist or "").strip()

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(str(file_path))

            # Clean out duplicate/conflicting casing tags that split albums in Navidrome
            for k in list(audio.keys()):
                if k.upper() in {
                    "ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST", "ARTIST",
                    "ALBUM", "TITLE", "TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS",
                    "DISCNUMBER", "DISCTOTAL", "DATE", "YEAR",
                    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ARTISTID",
                }:
                    del audio[k]

            if artist:
                audio["artist"] = artist
            if effective_album_artist:
                audio["albumartist"] = effective_album_artist
                audio["ALBUMARTIST"] = effective_album_artist
            if album:
                audio["album"] = album
            if title:
                audio["title"] = title
            if track_num:
                audio["tracknumber"] = str(track_num)
            if total_tracks:
                audio["totaltracks"] = str(total_tracks)
                audio["tracktotal"] = str(total_tracks)
            if disc_num:
                audio["discnumber"] = str(disc_num)
            if year:
                audio["date"] = str(year)
                audio["year"] = str(year)

            if cover_bytes:
                try:
                    pic = Picture()
                    pic.data = cover_bytes
                    pic.type = 3  # front cover
                    pic.mime = "image/jpeg"
                    audio.clear_pictures()
                    audio.add_picture(pic)
                except Exception as pe:
                    logger.debug("[QUEUE] FLAC cover embed note: %s", pe)

            audio.save()

        elif ext == ".mp3":
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TPE1, TPE2, TALB, TIT2, TRCK, TPOS, TDRC, APIC
            try:
                audio = MP3(str(file_path))
                if audio.tags is None:
                    audio.add_tags()
            except Exception:
                audio = MP3(str(file_path))
                audio.add_tags()

            tags = audio.tags
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=artist)
            if effective_album_artist:
                tags["TPE2"] = TPE2(encoding=3, text=effective_album_artist)
            if album:
                tags["TALB"] = TALB(encoding=3, text=album)
            if title:
                tags["TIT2"] = TIT2(encoding=3, text=title)
            if track_num:
                trck_str = f"{track_num}/{total_tracks}" if total_tracks else str(track_num)
                tags["TRCK"] = TRCK(encoding=3, text=trck_str)
            if disc_num:
                tags["TPOS"] = TPOS(encoding=3, text=str(disc_num))
            if year:
                tags["TDRC"] = TDRC(encoding=3, text=str(year))
            if cover_bytes:
                try:
                    tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes)
                except Exception as pe:
                    logger.debug("[QUEUE] MP3 cover embed note: %s", pe)
            audio.save()

        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(file_path))
            if artist:
                audio["\xa9ART"] = [artist]
            if effective_album_artist:
                audio["aART"] = [effective_album_artist]
            if album:
                audio["\xa9alb"] = [album]
            if title:
                audio["\xa9nam"] = [title]
            if track_num:
                audio["trkn"] = [(track_num, total_tracks or 0)]
            if disc_num:
                audio["disk"] = [(disc_num, 0)]
            if year:
                audio["\xa9day"] = [str(year)]
            if cover_bytes:
                try:
                    audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                except Exception as pe:
                    logger.debug("[QUEUE] MP4 cover embed note: %s", pe)
            audio.save()

        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
            audio = None
            try:
                audio = OggOpus(str(file_path))
            except Exception:
                try:
                    audio = OggVorbis(str(file_path))
                except Exception:
                    from mutagen import File as MutagenFile
                    audio = MutagenFile(str(file_path))

            if audio is not None:
                if artist:
                    audio["artist"] = [artist]
                if effective_album_artist:
                    audio["albumartist"] = [effective_album_artist]
                if album:
                    audio["album"] = [album]
                if title:
                    audio["title"] = [title]
                if track_num:
                    audio["tracknumber"] = [str(track_num)]
                if total_tracks:
                    audio["totaltracks"] = [str(total_tracks)]
                    audio["tracktotal"] = [str(total_tracks)]
                if disc_num:
                    audio["discnumber"] = [str(disc_num)]
                if year:
                    audio["date"] = [str(year)]
                audio.save()

    except Exception as e:
        logger.debug("[QUEUE] Tagging note for %s: %s", file_path.name, e)


def _get_setting(app: Flask, key: str, default: str = "") -> str:
    with app.app_context():
        s = db.session.get(AppSetting, key)
        return s.value if s else default


def _get_max_concurrent(app: Flask) -> int:
    try:
        val = int(_get_setting(app, "max_concurrent", "3"))
        return max(1, min(val, 10))
    except ValueError:
        return 3


def cancel_job(job_id: int) -> bool:
    with _queue_lock:
        cancel_requested_jobs.add(job_id)
    return True


def queue_track(app: Flask, track_id: int, source: str = "manual") -> Optional[DownloadJob]:
    """Queue a single track for download."""
    with app.app_context():
        track = db.session.get(Track, track_id)
        if not track:
            return None

        # Respect track disable/unmonitor setting
        if not getattr(track, "monitored", True) and source != "manual":
            logger.info("[QUEUE] Skipping unmonitored track %d: '%s'", track.id, track.title)
            return None

        album = track.album
        artist = album.artist if album else None
        if not artist:
            return None

        # Check existing active job
        existing = DownloadJob.query.filter_by(track_id=track.id).first()
        if existing:
            if existing.status in ("failed", "cancelled", "error"):
                existing.status = "queued"
                existing.progress = 0.0
                existing.error_message = None
                track.status = "queued"
                track.error_message = None
                db.session.commit()
                db.session.refresh(existing)
                return existing
            db.session.refresh(existing)
            return existing

        job = DownloadJob(
            track_id=track.id,
            album_id=album.id,
            artist_id=artist.id,
            item_type="track",
            album_spotify_id=str(album.deezer_id or ""),
            album_name=album.name,
            album_type=album.record_type,
            album_url="",
            cover_url=album.cover_url,
            status="queued",
            source=source,
        )
        track.status = "queued"
        track.error_message = None
        db.session.add(job)
        db.session.commit()
        db.session.refresh(job)
        logger.info("[QUEUE] Queued track %d: '%s - %s'", track.id, artist.name, track.title)
        return job


def queue_album(app: Flask, album_id: int, source: str = "manual") -> list[int]:
    """Queue all missing/failed tracks in an album, respecting monitoring preferences."""
    queued_ids = []
    with app.app_context():
        album = db.session.get(Album, album_id)
        if not album:
            return []
        if not getattr(album, "monitored", True) and source != "manual":
            logger.info("[QUEUE] Skipping unmonitored album %d: '%s'", album.id, album.name)
            return []
        for track in album.tracks.all():
            if not track.is_downloaded and getattr(track, "monitored", True):
                j = queue_track(app, track.id, source=source)
                if j:
                    queued_ids.append(j.id)
    return queued_ids


def queue_artist_missing(app: Flask, artist_id: int, source: str = "manual") -> int:
    """Queue all missing tracks for an artist, skipping unmonitored albums and tracks."""
    count = 0
    with app.app_context():
        artist = db.session.get(Artist, artist_id)
        if not artist or not getattr(artist, "monitored", True):
            return 0
        for album in artist.albums.all():
            if not getattr(album, "monitored", True):
                continue
            for track in album.tracks.all():
                if not track.is_downloaded and getattr(track, "monitored", True) and track.status not in ("queued", "downloading"):
                    j = queue_track(app, track.id, source=source)
                    if j:
                        count += 1
    return count


def _process_track_job(app: Flask, socketio: SocketIO, job_id: int):
    """Worker task for a single track download job."""
    with app.app_context():
        job = db.session.get(DownloadJob, job_id)
        if not job or job.status != "downloading":
            return

        track = job.track
        if not track:
            # Standalone job from Lidarr
            job.status = "failed"
            job.error_message = "No track record attached"
            db.session.commit()
            return

        album = track.album
        artist = album.artist if album else None
        artist_name = artist.name if artist else "Unknown Artist"
        album_name = album.name if album else "Unknown Album"
        album_cover_url = album.cover_url if album else None
        track_title = track.title
        isrc = track.isrc
        expected_duration = track.duration

        track_id = track.id
        album_id = album.id if album else 0
        artist_id = artist.id if artist else 0
        track_num = track.track_number or 0
        disc_num = track.disc_number or 1
        track_deezer_id = track.deezer_id

        quality_setting = _get_setting(app, "spotiflac_quality", "LOSSLESS")
        fallback_format = _get_setting(app, "ytdlp_format") or _get_setting(app, "spotdl_format", "flac")
        strictness_setting = _get_setting(app, "matching_strictness", "standard")
        max_duration_delta = STRICTNESS_DELTAS.get(strictness_setting, 8.0)
        reject_mismatches = _get_setting(app, "reject_mismatches", "true").lower() != "false"
        save_cover_setting = _get_setting(app, "save_cover_art", "true").lower() != "false"
        cover_filename_setting = _get_setting(app, "cover_art_filename", "cover.jpg")
        embed_cover_setting = _get_setting(app, "embed_cover_art", "true").lower() != "false"
        enable_spotiflac = _get_setting(app, "enable_spotiflac", "true").lower() != "false"
        enable_ytdlp = _get_setting(app, "enable_ytdlp", "true").lower() != "false"

        # Auto-resolve ISRC from Deezer if missing
        if not isrc and track_deezer_id:
            try:
                t_info = get_track_info(track_deezer_id)
                if t_info.get("isrc"):
                    isrc = t_info["isrc"]
                    track.isrc = isrc
                    db.session.commit()
                    logger.info("[QUEUE] Auto-resolved ISRC '%s' for '%s - %s'", isrc, artist_name, track_title)
            except Exception as ie:
                logger.debug("[QUEUE] Deezer ISRC lookup failed for track %d: %s", track_id, ie)

    if job_id in cancel_requested_jobs:
        _handle_cancellation(app, socketio, job_id, track_id)
        return

    # Check / create destination directory in library
    music_dir = Path(_get_setting(app, "music_path", "/music"))
    dest_dir = music_dir / _sanitize(artist_name) / _sanitize(album_name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Save album artwork as cover.jpg / folder.jpg in the album folder
    cover_bytes = _save_album_cover(dest_dir, album_cover_url, save_cover=save_cover_setting, cover_filename=cover_filename_setting)

    tmp_work_dir = DOWNLOADS_DIR / "work" / f"job_{job_id}_{int(time.time())}"
    tmp_work_dir.mkdir(parents=True, exist_ok=True)

    socketio.emit("download_progress", {
        "job_id": job_id,
        "track_id": track_id,
        "album_id": album_id,
        "artist_id": artist_id,
        "status": "downloading",
        "progress": 15.0,
        "title": track_title,
        "artist_name": artist_name,
    })

    verified_file: Optional[Path] = None
    file_meta: dict = {}
    failure_reasons = []

    try:
        # Step 0: Cross-Artist & Featuring Deduplication
        # If this track (or a matching version with same ISRC / Deezer ID / Title + Duration) was already downloaded under another artist or album, reuse the existing audio file without redownloading.
        if job_id not in cancel_requested_jobs:
            existing_match_path = None
            existing_match_size = None
            existing_match_dur = None
            existing_match_bitrate = None

            with app.app_context():
                existing_match: Optional[Track] = None
                # 1. Match by ISRC (highest accuracy)
                if isrc and len(str(isrc).strip()) == 12:
                    existing_match = Track.query.filter(
                        Track.id != track_id,
                        Track.isrc == isrc,
                        Track.is_downloaded == True,
                        Track.local_path.isnot(None),
                        Track.local_path != "",
                    ).first()

                # 2. Match by Deezer ID
                if not existing_match and track_deezer_id:
                    existing_match = Track.query.filter(
                        Track.id != track_id,
                        Track.deezer_id == track_deezer_id,
                        Track.is_downloaded == True,
                        Track.local_path.isnot(None),
                        Track.local_path != "",
                    ).first()

                # 3. Match by normalized title and duration
                if not existing_match and track_title and expected_duration:
                    candidates = Track.query.filter(
                        Track.id != track_id,
                        Track.is_downloaded == True,
                        Track.local_path.isnot(None),
                        Track.local_path != "",
                    ).all()
                    norm_current = re.sub(r"[^\w\s]", "", track_title.lower()).strip()
                    for cand in candidates:
                        if not cand.title:
                            continue
                        norm_cand = re.sub(r"[^\w\s]", "", cand.title.lower()).strip()
                        if (norm_current == norm_cand or norm_current in norm_cand or norm_cand in norm_current):
                            if cand.duration and abs(cand.duration - expected_duration) <= 3.0:
                                if cand.local_path and Path(cand.local_path).exists():
                                    existing_match = cand
                                    break

                if existing_match and existing_match.local_path:
                    existing_match_path = existing_match.local_path
                    existing_match_size = existing_match.size_bytes
                    existing_match_dur = existing_match.duration
                    existing_match_bitrate = existing_match.bitrate

            if existing_match_path:
                src_file = Path(existing_match_path)
                if src_file.exists():
                    ext = src_file.suffix
                    disc_prefix = f"{disc_num}-" if disc_num and disc_num > 1 else ""
                    track_num_prefix = f"{disc_prefix}{track_num:02d}. " if track_num else ""
                    final_filename = f"{track_num_prefix}{_sanitize(track_title)}{ext}"
                    target_file = dest_dir / final_filename

                    if target_file.resolve() != src_file.resolve():
                        if target_file.exists():
                            try:
                                target_file.unlink()
                            except OSError:
                                pass
                        # Always create an independent copy rather than a hardlink so embedded tags
                        # (Album, Album Artist, Track Number) belong strictly to this album without cross-album tag collisions.
                        shutil.copy2(str(src_file), str(target_file))
                        logger.info("[QUEUE] Copied existing file for '%s - %s' from '%s'", artist_name, track_title, src_file)

                    verified_file = target_file
                    file_meta = {
                        "size_bytes": existing_match_size or (target_file.stat().st_size if target_file.exists() else 0),
                        "duration": existing_match_dur or expected_duration,
                        "bitrate": existing_match_bitrate,
                    }

        # Step 1: Resolve Spotify link (ISRC-first) if SpotiFLAC is enabled and not already resolved from library
        if not verified_file and enable_spotiflac and job_id not in cancel_requested_jobs:
            spotify_url = resolve_spotify_url(track_title, artist_name, album_name, isrc=isrc)
        else:
            spotify_url = None

        # Step 2: Primary Downloader -> SpotiFLAC
        if not verified_file and enable_spotiflac and spotify_url and job_id not in cancel_requested_jobs:
            socketio.emit("download_progress", {"job_id": job_id, "track_id": track_id, "progress": 35.0, "status": "downloading"})
            ok, downloaded_file, err = download_track_spotiflac(spotify_url, tmp_work_dir, quality=quality_setting)
            if ok and downloaded_file:
                # Step 3: Verify audio file with strict duration checking
                v_ok, v_err, meta = verify_audio_file(
                    downloaded_file,
                    expected_duration_seconds=expected_duration,
                    max_duration_delta=max_duration_delta,
                )
                if v_ok:
                    verified_file = downloaded_file
                    file_meta = meta
                else:
                    failure_reasons.append(f"SpotiFLAC verification failed: {v_err}")
            else:
                failure_reasons.append(f"SpotiFLAC failed: {err}")
        elif not enable_spotiflac and not verified_file:
            logger.info("[QUEUE] SpotiFLAC disabled in settings, skipping for '%s - %s'", artist_name, track_title)

        # Step 4: Fallback Downloader -> yt-dlp with intelligent candidate selection & YouTube Music Topic prioritization
        if not verified_file and enable_ytdlp and job_id not in cancel_requested_jobs:
            socketio.emit("download_progress", {"job_id": job_id, "track_id": track_id, "progress": 60.0, "status": "downloading"})
            logger.info("[QUEUE] Attempting yt-dlp candidate search for '%s - %s'", artist_name, track_title)

            ok, downloaded_file, err = download_track_ytdlp(
                f"{artist_name} - {track_title}",
                tmp_work_dir,
                output_format=fallback_format,
                artist_name=artist_name,
                track_title=track_title,
                expected_duration=expected_duration,
            )
            if ok and downloaded_file:
                v_ok, v_err, meta = verify_audio_file(
                    downloaded_file,
                    expected_duration_seconds=expected_duration,
                    max_duration_delta=max_duration_delta,
                    delete_on_failure=True,
                )
                if v_ok:
                    verified_file = downloaded_file
                    file_meta = meta
                else:
                    failure_reasons.append(f"yt-dlp verification failed: {v_err}")
            else:
                failure_reasons.append(f"yt-dlp failed: {err}")
        elif not enable_ytdlp and not verified_file:
            logger.info("[QUEUE] yt-dlp disabled in settings, skipping for '%s - %s'", artist_name, track_title)
            if not enable_spotiflac:
                failure_reasons.append("Both SpotiFLAC and yt-dlp engines are disabled in settings")

        if job_id in cancel_requested_jobs:
            _handle_cancellation(app, socketio, job_id, track_id, tmp_work_dir)
            return

        # Step 5: Finalize, clean up superseded duplicates, tag, and move file into /music
        with app.app_context():
            job = db.session.get(DownloadJob, job_id)
            track_rec = db.session.get(Track, track_id)
            album_rec = db.session.get(Album, album_id) if album_id else None

            if verified_file and verified_file.exists():
                album_year = album_rec.year if album_rec else None
                total_tracks_val = album_rec.tracks.count() if album_rec else None
                disc_num_val = track_rec.disc_number if track_rec else 1

                ext = verified_file.suffix
                disc_prefix = f"{disc_num_val}-" if (disc_num_val and disc_num_val > 1) else ""
                track_num_prefix = f"{disc_prefix}{track_num:02d}. " if track_num else ""
                final_filename = f"{track_num_prefix}{_sanitize(track_title)}{ext}"
                final_dest = dest_dir / final_filename

                # Clean up any older/superseded files for this exact track position to prevent duplicate tracks in Navidrome
                try:
                    for old_f in dest_dir.iterdir():
                        if old_f.is_file() and old_f.suffix.lower() in AUDIO_EXTENSIONS:
                            if old_f.resolve() != final_dest.resolve():
                                if track_num and (old_f.name.startswith(f"{track_num:02d}. ") or old_f.name.startswith(f"{disc_prefix}{track_num:02d}. ")):
                                    try:
                                        old_f.unlink()
                                        logger.info("[QUEUE] Cleaned up older audio file: %s", old_f.name)
                                    except OSError:
                                        pass
                except Exception as ce:
                    logger.debug("[QUEUE] Duplicate cleanup note: %s", ce)

                if verified_file.resolve() != final_dest.resolve():
                    if final_dest.exists():
                        try:
                            final_dest.unlink()
                        except OSError:
                            pass
                    shutil.move(str(verified_file), str(final_dest))

                # Embed clean metadata tags with album artist and optional artwork to guarantee seamless Navidrome indexing
                _tag_audio_file(
                    final_dest,
                    artist=artist_name,
                    album=album_name,
                    title=track_title,
                    track_num=track_num,
                    year=album_year,
                    album_artist=artist_name,
                    disc_num=disc_num_val,
                    total_tracks=total_tracks_val,
                    cover_bytes=cover_bytes if embed_cover_setting else None,
                )
                rel_path = str(final_dest.relative_to(music_dir))

                if track_rec:
                    track_rec.is_downloaded = True
                    track_rec.status = "completed"
                    track_rec.progress = 100.0
                    track_rec.local_path = str(final_dest)
                    track_rec.file_path = rel_path
                    track_rec.file_format = ext.lstrip(".")
                    track_rec.size_bytes = file_meta.get("size_bytes", final_dest.stat().st_size)
                    track_rec.duration = file_meta.get("duration") or expected_duration
                    track_rec.bitrate = file_meta.get("bitrate")
                    track_rec.error_message = None

                if job:
                    job.status = "completed"
                    job.progress = 100.0
                    job.tracks_completed = 1
                    job.error_message = None

                # Update Album stats
                if album_rec:
                    album_tracks = album_rec.tracks.all()
                    downloaded_count = sum(1 for t in album_tracks if t.is_downloaded)
                    album_rec.is_downloaded = downloaded_count == len(album_tracks)
                    album_rec.size_bytes = sum(t.size_bytes or 0 for t in album_tracks)
                    album_rec.local_path = str(dest_dir)

                db.session.commit()
                logger.info("[QUEUE] Download succeeded for '%s - %s' -> %s", artist_name, track_title, final_dest)

                # Trigger Navidrome automatic scan if configured
                try:
                    trigger_navidrome_scan(app)
                except Exception as ne:
                    logger.debug("[QUEUE] Navidrome auto-scan trigger note: %s", ne)

                socketio.emit("download_progress", {
                    "job_id": job_id,
                    "track_id": track_id,
                    "album_id": album_id,
                    "artist_id": artist_id,
                    "status": "completed",
                    "progress": 100.0,
                    "title": track_title,
                    "local_path": str(final_dest),
                })
            else:
                combined_err = " | ".join(failure_reasons) or "Download failed"
                if track_rec:
                    track_rec.status = "failed"
                    track_rec.progress = 0.0
                    track_rec.error_message = combined_err
                if job:
                    job.status = "failed"
                    job.progress = 0.0
                    job.error_message = combined_err
                db.session.commit()

                logger.warning("[QUEUE] Download failed for '%s - %s': %s", artist_name, track_title, combined_err)
                socketio.emit("download_progress", {
                    "job_id": job_id,
                    "track_id": track_id,
                    "album_id": album_id,
                    "artist_id": artist_id,
                    "status": "failed",
                    "progress": 0.0,
                    "error_message": combined_err,
                })

    except Exception as e:
        logger.exception("[QUEUE] Unexpected exception during job %d: %s", job_id, e)
        with app.app_context():
            job = db.session.get(DownloadJob, job_id)
            track_rec = db.session.get(Track, track_id)
            if job:
                job.status = "failed"
                job.error_message = str(e)
            if track_rec:
                track_rec.status = "failed"
                track_rec.error_message = str(e)
            db.session.commit()

    finally:
        try:
            if tmp_work_dir.exists():
                shutil.rmtree(str(tmp_work_dir))
        except OSError:
            pass
        with _queue_lock:
            cancel_requested_jobs.discard(job_id)


def _handle_cancellation(app: Flask, socketio: SocketIO, job_id: int, track_id: int, tmp_dir: Optional[Path] = None):
    if tmp_dir and tmp_dir.exists():
        try:
            shutil.rmtree(str(tmp_dir))
        except OSError:
            pass

    with app.app_context():
        job = db.session.get(DownloadJob, job_id)
        track = db.session.get(Track, track_id)
        if job:
            job.status = "cancelled"
            job.error_message = "Cancelled by user"
        if track:
            track.status = "missing"
            track.error_message = None
        db.session.commit()

    socketio.emit("download_progress", {
        "job_id": job_id,
        "track_id": track_id,
        "status": "cancelled",
        "progress": 0.0,
    })


def start_queue_worker(app: Flask, socketio: SocketIO):
    """Background download queue worker greenlet."""
    global _executor, _running_futures
    logger.info("[QUEUE] Starting queue worker loop")
    _executor = ThreadPoolExecutor(max_workers=_get_max_concurrent(app))

    while True:
        try:
            max_workers = _get_max_concurrent(app)
            if _executor._max_workers != max_workers:
                _executor = ThreadPoolExecutor(max_workers=max_workers)

            _running_futures = {f for f in _running_futures if not f.done()}

            if len(_running_futures) < max_workers:
                with app.app_context():
                    job = DownloadJob.query.filter_by(status="queued").order_by(DownloadJob.created_at).first()
                    if job:
                        job.status = "downloading"
                        job.error_message = None
                        if job.track:
                            job.track.status = "downloading"
                        db.session.commit()

                        job_id = job.id
                        future = _executor.submit(_process_track_job, app, socketio, job_id)
                        _running_futures.add(future)

            gevent.sleep(1.5)
        except RuntimeError as e:
            if "interpreter shutdown" in str(e).lower() or "cannot schedule new futures" in str(e).lower():
                logger.info("[QUEUE] Interpreter shutting down; exiting worker loop.")
                break
            logger.exception("[QUEUE] Queue worker loop runtime error")
            gevent.sleep(4)
        except Exception:
            logger.exception("[QUEUE] Queue worker loop encountered error")
            gevent.sleep(4)
