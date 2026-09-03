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
from typing import Optional, Set, Tuple
import gevent
from flask import Flask
from flask_socketio import SocketIO

from models import Album, AppSetting, Artist, DownloadJob, Track, db
from services.verifier_service import STRICTNESS_DELTAS, verify_audio_file
import requests

logger = logging.getLogger("fnack.queue")

DOWNLOADS_DIR = Path("/downloads")
MUSIC_DIR = Path("/music")
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}

cancel_requested_jobs: Set[int] = set()


# ---------------------------------------------------------------------------
# Phase 2/3 download chain (PR 3 + PR 4 + Phase 3)
#
# The queue drives the `download.track` capability through DownloadService
# (services/download_service.py) — the application service owns provider
# resolution, the sequential-fallback policy, and the per-provider invocation
# (SDK DownloadRequest for fnack.spotiflac / fnack.ytdlp, legacy signature for
# pre-SDK providers, all via the manager's ProviderExecutor boundary). The
# queue supplies the per-provider verification policy (verify hook) and the
# progress events; core never names a provider.
# ---------------------------------------------------------------------------

def _download_via_chain(spotify_url: str, work_dir: Path,
                                     quality: Optional[str] = None,
                                     delay: Optional[float] = None):
    """Manual-download path (Phase 3): download a Spotify URL through the
    download.track capability via DownloadService (the application service
    owns provider policy) — NOT a direct core->service call. Returns
    (success, file_path, error)."""
    from services.download_service import CapabilityUnavailable, DownloadService
    from plugins.base import TrackRef
    from fnack.plugin_api.models import DownloadRequest
    request = DownloadRequest(
        track=TrackRef(id=0, title="", artist_name="", album_name="",
                       spotify_url=spotify_url),
        destination=work_dir,
        quality=quality,
    )
    try:
        result = DownloadService().download(request, stop_on_first_attempt=True)
    except CapabilityUnavailable:
        return False, None, "no download provider available"
    if result and result.success and result.path:
        return True, result.path, None
    return False, None, (result.message if result else "download failed")


def _download_via_ytdlp_provider(query_or_url: str, work_dir: Path,
                                 output_format: Optional[str] = None,
                                 cookies_path: Optional[str] = None,
                                 check_duration: bool = True,
                                 artist_name: Optional[str] = None,
                                 track_title: Optional[str] = None,
                                 expected_duration: Optional[float] = None):
    """Manual-download path (Phase 3): download a raw query/URL (YouTube,
    SoundCloud, or a search string) through the download.track capability via
    DownloadService (the application service owns provider policy) — NOT a
    direct core->service call. Returns (success, file_path, error)."""
    from services.download_service import CapabilityUnavailable, DownloadService
    from plugins.base import TrackRef
    from fnack.plugin_api.models import DownloadRequest
    request = DownloadRequest(
        track=TrackRef(id=0, title=track_title or "", artist_name=artist_name or "",
                       album_name="", duration=expected_duration),
        destination=work_dir,
        format=output_format,
        cookies_path=cookies_path,
        query=query_or_url,
        check_duration=check_duration,
    )
    try:
        result = DownloadService().download(request, stop_on_first_attempt=True)
    except CapabilityUnavailable:
        return False, None, "no download provider available"
    if result and result.success and result.path:
        return True, result.path, None
    return False, None, (result.message if result else "download failed")


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
    genre: Optional[str] = None,
) -> None:
    """
    Embed clean, uniform metadata tags (artist, album artist, album, title, track number, disc, year, genre)
    and front cover artwork across all formats using Mutagen, eliminating casing conflicts for Navidrome.
    """
    try:
        ext = file_path.suffix.lower()
        effective_album_artist = (album_artist or artist or "").strip()
        effective_genre = (genre or "").strip()

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(str(file_path))

            # Clean out duplicate/conflicting casing tags that split albums in Navidrome
            for k in list(audio.keys()):
                if k.upper() in {
                    "ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST", "ARTIST",
                    "ALBUM", "TITLE", "TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS",
                    "DISCNUMBER", "DISCTOTAL", "DATE", "YEAR",
                    # Per-track dates/IDs from the source downloader would give
                    # every song its own release date and make Navidrome split
                    # the album into one row per song — always strip them.
                    "ORIGINALDATE", "RELEASEDATE", "TDOR", "TDRL",
                    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ARTISTID",
                    "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_RELEASETRACKID",
                    "MUSICBRAINZ_ALBUMSTATUS", "MUSICBRAINZ_ALBUMTYPE",
                    # Songwriter/producer/participant credits become phantom
                    # artists in Navidrome — strip them everywhere.
                    "COMPOSER", "COMPOSER_ARTIST", "WRITER", "LYRICIST", "TEXTWRITER",
                    "PRODUCER", "ARRANGER", "PERFORMER", "MUSICIANCREDITS",
                    "ENGINEER", "MIXER", "PUBLISHER", "LABEL", "REMIXER", "CONDUCTOR",
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
            if effective_genre:
                audio["genre"] = effective_genre

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
            # Strip per-track release/original dates and songwriter/participant
            # credits (Navidrome splits albums by dates and invents phantom
            # artists from composer/producer credits) before writing the
            # uniform ones.
            for fid in ("TDRL", "TDOR", "TCOM", "TEXT", "TPE3", "TPE4", "TENC", "TIT3"):
                tags.delall(fid)
            for k in [k for k in tags.keys() if k.startswith("TXXX:")]:
                tags.delall(k)
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
            if effective_genre:
                from mutagen.id3 import TCON
                tags["TCON"] = TCON(encoding=3, text=effective_genre)
            if cover_bytes:
                try:
                    tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes)
                except Exception as pe:
                    logger.debug("[QUEUE] MP3 cover embed note: %s", pe)
            audio.save()

        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(file_path))
            # Per-track dates and songwriter credits split albums / invent
            # phantom artists in Navidrome — drop any date/writer/custom
            # iTunes atoms before writing the uniform ones.
            for k in list(audio.keys()):
                if "day" in k.lower() or "date" in k.lower() or "wrt" in k.lower():
                    del audio[k]
                if k.startswith("----:com.apple.itunes:"):
                    del audio[k]
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
            if effective_genre:
                audio["\xa9gen"] = [effective_genre]
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
                # Strip per-track dates/IDs and songwriter/participant credits
                # (Navidrome album splits + phantom artists) before writing the
                # uniform album date.
                for k in list(audio.keys()):
                    if k.upper() in {
                        "ORIGINALDATE", "RELEASEDATE", "TDOR", "TDRL",
                        "COMPOSER", "COMPOSER_ARTIST", "WRITER", "LYRICIST",
                        "TEXTWRITER", "PRODUCER", "ARRANGER", "PERFORMER",
                        "MUSICIANCREDITS", "ENGINEER", "MIXER", "PUBLISHER",
                        "LABEL", "REMIXER", "CONDUCTOR",
                    }:
                        del audio[k]
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
                if effective_genre:
                    audio["genre"] = [effective_genre]
                if cover_bytes:
                    try:
                        # Ogg formats embed pictures via the FLAC Picture block
                        from mutagen.flac import Picture
                        import base64
                        pic = Picture()
                        pic.data = cover_bytes
                        pic.type = 3  # front cover
                        pic.mime = "image/jpeg"
                        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
                    except Exception as pe:
                        logger.debug("[QUEUE] Opus cover embed note: %s", pe)
                audio.save()

    except Exception as e:
        # file_path may be a str or Path depending on the caller — never
        # crash the error handler itself (that masked the real tagging error).
        try:
            fname = Path(file_path).name
        except Exception:
            fname = str(file_path)
        logger.warning("[QUEUE] Tagging note for %s: %s", fname, e)
        raise


def _get_setting(app: Flask, key: str, default: str = "") -> str:
    with app.app_context():
        s = db.session.get(AppSetting, key)
        return s.value if s else default


def _get_max_concurrent(app: Flask) -> int:
    try:
        val = int(_get_setting(app, "max_concurrent", "1"))
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
            album_external_id=str(album.external_id or ""),
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


def _verify_or_rescue(app, downloaded_file, expected_duration, artist_name, track_title,
                      max_duration_delta, reject_mismatches, enable_duration_check):
    """Verify a downloaded file via VerificationService (Phase 3 — the
    provider-neutral verification policy combines metadata + fingerprint
    evidence; the queue's AcoustID-specific rescue semantics are preserved by
    the service's fingerprint handling, but core has no provider branch).

    Returns (v_ok, v_err, meta, caution_info_or_None):
      * v_ok True,  caution None  -> verified (or fingerprint-confirmed "right
                                     file, wrong tags" — finalize retags it)
      * v_ok False, caution set   -> evidence says it is a DIFFERENT song:
                                     keep the file, caller flags the track
                                     with `caution_info` (user decides later)
      * v_ok False, caution None  -> normal rejection; file deleted per
                                     reject_mismatches
    """
    from services.verification_service import VerificationService
    from fnack.plugin_api.models import TrackRef

    try:
        result = VerificationService().verify(
            TrackRef(id=0, title=track_title or "", artist_name=artist_name or "",
                     album_name="", duration=expected_duration if enable_duration_check else None),
            Path(downloaded_file),
        )
    except Exception as exc:
        logger.debug("[QUEUE] VerificationService failed: %s", exc)
        result = None

    if result is None:
        if reject_mismatches:
            try:
                if os.path.isfile(downloaded_file):
                    os.remove(downloaded_file)
            except OSError:
                pass
        return False, "verification unavailable", {}, None

    if result.status == "verified":
        cm = result.canonical_match
        if cm and cm.title and (cm.title.lower() != (track_title or "").lower()):
            logger.info("[QUEUE] Fingerprint confirmed '%s - %s' (right file, wrong tags) — accepting",
                        artist_name, track_title)
        return True, "; ".join(result.reasons) or "verified", {}, None

    if result.status == "mismatch":
        cm = result.canonical_match
        logger.warning(
            "[QUEUE] Verification mismatch for '%s - %s': matched to %r — keeping file, flagging for user",
            artist_name, track_title, (cm.title if cm else None),
        )
        caution = {
            "matched_title": cm.title if cm else None,
            "matched_artists": [cm.artist] if cm and cm.artist else [],
            "score": (cm.score if cm else 0.0) or 0.0,
        }
        return False, "; ".join(result.reasons) or "mismatch", {}, caution

    # uncertain / provider_error: normal rejection
    if reject_mismatches:
        try:
            if os.path.isfile(downloaded_file):
                os.remove(downloaded_file)
        except OSError:
            pass
    return False, "; ".join(result.reasons) or "not verified", {}, None


def _process_track_job(app: Flask, socketio: SocketIO, job_id: int):
    """Worker task for a single track download job.

    The ENTIRE body below runs inside one app context. This function executes
    on a ThreadPoolExecutor worker — its own thread/greenlet — and gevent makes
    Flask's app context greenlet-local, so nothing is inherited from the queue
    loop. Providers read plugin settings through the DB, so every provider
    invocation in the job must run with a context active; the whole body is
    indented under the single `with` below, by construction.
    """
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
        track_external_id = track.external_id

        quality_setting = _get_setting(app, "spotiflac_quality", "LOSSLESS")
        fallback_format = _get_setting(app, "ytdlp_format") or _get_setting(app, "spotdl_format", "flac")
        strictness_setting = _get_setting(app, "matching_strictness", "standard")
        max_duration_delta = STRICTNESS_DELTAS.get(strictness_setting, 8.0)
        reject_mismatches = _get_setting(app, "reject_mismatches", "true").lower() != "false"
        enable_duration_check = _get_setting(app, "enable_duration_check", "true").lower() != "false"
        save_cover_setting = _get_setting(app, "save_cover_art", "true").lower() != "false"
        cover_filename_setting = _get_setting(app, "cover_art_filename", "cover.jpg")
        embed_cover_setting = _get_setting(app, "embed_cover_art", "true").lower() != "false"
        cookies_path = _get_setting(app, "youtube_cookies_path", "/config/cookies.txt")
        prefer_yt_music = _get_setting(app, "youtube_source", "youtube_music").lower() == "youtube_music"
        # When the duration check is disabled, skip the duration comparison entirely
        # (any valid audio file is accepted) while keeping basic file validation.
        verify_expected_duration = expected_duration if enable_duration_check else None
        flagged_caution = None  # set when AcoustID flags a kept-but-different file

        # Auto-resolve ISRC and genre from a metadata provider when missing
        # (through MetadataService, capability-based — track.metadata is
        # resolved via the registry; the provider that owns the external id
        # interprets it).
        track_genre = track.genre or None
        if (not isrc or not track_genre) and track_external_id:
            try:
                from services.metadata_service import MetadataService
                t_info = MetadataService().get_track_metadata(str(track_external_id)) or {}
                if t_info.get("isrc") and not isrc:
                    isrc = t_info["isrc"]
                    track.isrc = isrc
                if t_info.get("genre") and not track_genre:
                    track_genre = t_info["genre"]
                    track.genre = track_genre
                if t_info.get("isrc") or t_info.get("genre"):
                    db.session.commit()
                if t_info.get("isrc"):
                    logger.info("[QUEUE] Auto-resolved ISRC '%s' for '%s - %s'", isrc, artist_name, track_title)
                if t_info.get("genre"):
                    logger.info("[QUEUE] Auto-resolved genre '%s' for '%s - %s'", track_genre, artist_name, track_title)
            except Exception as ie:
                logger.debug("[QUEUE] ISRC/genre metadata lookup failed for track %d: %s", track_id, ie)

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
            # If this track (or a matching version with same ISRC / external id / title + duration) was already downloaded under another artist or album, reuse the existing audio file without redownloading.
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

                    # 2. Match by external id
                    if not existing_match and track_external_id:
                        existing_match = Track.query.filter(
                            Track.id != track_id,
                            Track.external_id == track_external_id,
                            Track.is_downloaded == True,
                            Track.local_path.isnot(None),
                            Track.local_path != "",
                        ).first()

                    # 3. Match by normalized title and duration
                    if not existing_match and track_title and expected_duration:
                        norm_current = re.sub(r"[^\w\s]", "", track_title.lower()).strip()
                        if len(norm_current) >= 4:
                            # Narrow with a LIKE filter instead of scanning the whole library
                            candidates = Track.query.filter(
                                Track.id != track_id,
                                Track.is_downloaded == True,
                                Track.local_path.isnot(None),
                                Track.local_path != "",
                                Track.title.ilike(f"%{track_title[:60]}%"),
                            ).limit(50).all()
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
                            # copy2 overwrites an existing target on POSIX; no explicit unlink
                            # needed (an unlink here triggered the folder watcher's false
                            # "missing" marking). Always create an independent copy rather than
                            # a hardlink so embedded tags (Album, Album Artist, Track Number)
                            # belong strictly to this album without cross-album tag collisions.
                            try:
                                shutil.copy2(str(src_file), str(target_file))
                            except shutil.SameFileError:
                                logger.info("[QUEUE] Source and target are the same file for '%s - %s'; already in place", artist_name, track_title)
                            logger.info("[QUEUE] Copied existing file for '%s - %s' from '%s'", artist_name, track_title, src_file)

                        verified_file = target_file
                        file_meta = {
                            "size_bytes": existing_match_size or (target_file.stat().st_size if target_file.exists() else 0),
                            "duration": existing_match_dur or expected_duration,
                            "bitrate": existing_match_bitrate,
                        }

            # Step 1: Resolve Spotify link (ISRC-first) if any download provider is
            # enabled (Phase 2/3: no per-provider gate — a disabled plugin simply
            # isn't a download.track provider). Routed through MetadataService
            # (track.resolve capability — the fnack.spotify plugin serves it) so
            # the resolution logic lives behind the plugin boundary; no direct
            # provider-service call and no hidden fallback — if no track.resolve
            # provider is enabled the URL stays None and the chain proceeds
            # without a Spotify link (spotiflac can_handle gates on it).
            spotify_url = None
            if not verified_file and job_id not in cancel_requested_jobs:
                from plugins.manager import plugin_manager as _pm0
                _resolve_enabled = bool(_pm0 is not None and _pm0.has_capability("download.track"))
                if _resolve_enabled:
                    try:
                        from services.metadata_service import MetadataService
                        spotify_url = MetadataService().resolve_track_url(
                            track_title,
                            artist_name,
                            album_name=album_name,
                            isrc=isrc,
                            track_number=track_num,
                        )
                    except Exception as e:
                        logger.debug("[METADATA] resolve_track_url failed: %s", e)

            # Steps 2-4: Downloader chain (Phase 3 — DownloadService owns the
            # download.track resolution + provider policy; the queue orchestrates).
            # Priority-ordered providers from the capability registry; each is
            # verified right after its download via the verify hook (preserves the
            # AcoustID rescue semantics and which failure surfaces first). A
            # plugin disabled in Settings → Plugins is skipped by the registry
            # itself — no per-provider gate exists anywhere.
            from plugins.base import TrackRef
            from fnack.plugin_api.models import DownloadRequest
            from services.download_service import (
                CapabilityUnavailable,
                DownloadService,
                VerifyVerdict,
            )

            track_ref = TrackRef(
                id=track_id,
                title=track_title,
                artist_name=artist_name,
                album_name=album_name,
                isrc=isrc,
                duration=expected_duration,
                spotify_url=spotify_url,
                external_id=track_external_id,
                disc_number=disc_num or 1,   # captured before the dedup app_context closed
                track_number=track_num,
            )

            def _verify_hook(result):
                """Per-provider verification policy (queue-owned until
                VerificationService lands): accept / flag / reject."""
                v_ok, v_err, meta, flagged = _verify_or_rescue(
                    app, result.path, verify_expected_duration,
                    artist_name, track_title, max_duration_delta,
                    reject_mismatches, enable_duration_check,
                )
                if v_ok:
                    return VerifyVerdict("accept", meta=meta)
                if flagged:
                    return VerifyVerdict("flag", meta=meta, caution=flagged)
                return VerifyVerdict("reject", error=v_err)

            def _on_progress(idx, provider):
                # Preserve the old UI feel: 35% for the primary (first) downloader,
                # 60% for fallbacks.
                manifest = getattr(provider, "manifest", None)
                name = getattr(manifest, "name", None) or "provider"
                socketio.emit("download_progress", {"job_id": job_id, "track_id": track_id,
                                                    "progress": 35.0 if idx == 0 else 60.0,
                                                    "status": "downloading"})
                logger.info("[QUEUE] Attempting %s for '%s - %s'", name, artist_name, track_title)

            request = DownloadRequest(
                track=track_ref,
                destination=tmp_work_dir,
                quality=quality_setting,
                format=fallback_format,
                audio_source="youtube_music" if prefer_yt_music else "youtube",
                cookies_path=cookies_path,
                check_duration=enable_duration_check,
            )
            if verified_file or job_id in cancel_requested_jobs:
                pass  # dedup copy already produced a file / cancelled — skip chain
            else:
                try:
                    result = DownloadService().download(
                        request, verify=_verify_hook, on_progress=_on_progress)
                except CapabilityUnavailable:
                    result = None
                if result is not None and result.success and result.path:
                    downloaded_file = result.path
                    verified_file = downloaded_file
                    file_meta = dict((result.metadata or {}).get("file_meta") or {})
                    flagged_caution = (result.metadata or {}).get("caution")
                    if flagged_caution:
                        # Plugin framework (Phase 4): notify webhook plugins when
                        # AcoustID flags a kept-but-different file.
                        try:
                            from plugins.manager import plugin_manager
                            if plugin_manager is not None:
                                plugin_manager.event_bus.emit(
                                    "track.caution_flagged",
                                    track_id=track_id,
                                    matched_title=flagged_caution.get("matched_title"),
                                    matched_artist=(flagged_caution.get("matched_artists") or [None])[0],
                                    score=flagged_caution.get("score"),
                                )
                        except Exception:
                            logger.debug("[QUEUE] plugin caution event skipped", exc_info=True)
                else:
                    err = (result.message if result is not None
                           else "No enabled download providers are available")
                    failure_reasons.append(err)

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

                    # Move the new file into place. shutil.move overwrites an existing
                    # file at the destination on POSIX, so no explicit unlink is needed —
                    # an explicit unlink here is what made the folder watcher see a
                    # deletion of a DB-referenced file and mark the track missing.
                    if verified_file.resolve() != final_dest.resolve():
                        try:
                            shutil.move(str(verified_file), str(final_dest))
                        except shutil.SameFileError:
                            logger.info("[QUEUE] Work file and destination are the same file for '%s - %s'; already in place", artist_name, track_title)

                    # Embed clean metadata tags with album artist and optional artwork to guarantee seamless Navidrome indexing.
                    # EXCEPT AcoustID-flagged files: the audio is a DIFFERENT song, so we
                    # deliberately keep its original tags (Navidrome shows what it really
                    # is) and let the user keep/delete via the caution flag.
                    if not flagged_caution:
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
                            genre=track_genre,
                        )
                    rel_path = str(final_dest.relative_to(music_dir))

                    was_downloaded = bool(track_rec.is_downloaded) if track_rec else False
                    if track_rec:
                        track_rec.is_downloaded = True
                        track_rec.status = "completed"
                        track_rec.progress = 100.0
                        track_rec.local_path = str(final_dest)
                        track_rec.file_path = rel_path
                        track_rec.file_format = ext.lstrip(".")
                        track_rec.size_bytes = file_meta.get("size_bytes", final_dest.stat().st_size)
                        # Keep the official expected duration as the verification baseline;
                        # never overwrite it with the downloaded file's actual duration
                        # (that would mask future mismatches). Only fill in if unknown.
                        if not track_rec.duration:
                            track_rec.duration = file_meta.get("duration") or expected_duration
                        track_rec.bitrate = file_meta.get("bitrate")
                        track_rec.error_message = None
                        if flagged_caution:
                            import json as _json
                            track_rec.caution = True
                            track_rec.caution_info = _json.dumps(flagged_caution)
                        elif not flagged_caution:
                            # Plugin framework (INTEGRATION.md §5): additive event emission
                            # on successful verification (not for AcoustID-flagged files —
                            # those are a different song, not verified).
                            try:
                                from plugins.manager import plugin_manager
                                if plugin_manager is not None:
                                    plugin_manager.event_bus.emit("track.verified", track_id=track_id)
                            except Exception:
                                logger.debug("[QUEUE] plugin verified-event emission skipped", exc_info=True)

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

                    # Phase 1 (scale-to-millions): keep the denormalized per-artist
                    # counters in sync. Counters follow album.artist_id — the same
                    # grouping the old GROUP BY used. Guard False→True so
                    # re-downloads of an already-downloaded track don't double-count.
                    try:
                        from services.counters_service import on_track_downloaded
                        if album_rec and album_rec.artist_id and not was_downloaded:
                            on_track_downloaded(album_rec.artist_id, is_downloaded=True)
                    except Exception:
                        logger.debug("[QUEUE] counter update skipped", exc_info=True)

                    db.session.commit()
                    logger.info("[QUEUE] Download succeeded for '%s - %s' -> %s", artist_name, track_title, final_dest)

                    # Plugin framework (INTEGRATION.md §5): additive event emission —
                    # no existing behavior changes; event_hook/fingerprint plugins can
                    # react to real downloads.
                    try:
                        from plugins.manager import plugin_manager
                        if plugin_manager is not None:
                            plugin_manager.event_bus.emit("track.after_download", track_id=track_id)
                            plugin_manager.event_bus.emit(
                                "queue.job_completed",
                                job_id=job_id, track_id=track_id,
                                title=track_title, artist_name=artist_name,
                                album_name=album_name,
                            )
                    except Exception:
                        logger.debug("[QUEUE] plugin event emission skipped (plugin_manager not ready)", exc_info=True)

                    # Clean up superseded files for this exact track position (e.g. an
                    # old .opus replaced by a lossless .flac). This runs AFTER the DB
                    # commit so no track row references the deleted file anymore and the
                    # folder watcher cannot mistake the deletion for a user removal.
                    try:
                        for old_f in dest_dir.iterdir():
                            if old_f.is_file() and old_f.suffix.lower() in AUDIO_EXTENSIONS:
                                if old_f.resolve() != final_dest.resolve():
                                    if track_num and (old_f.name.startswith(f"{track_num:02d}. ") or old_f.name.startswith(f"{disc_prefix}{track_num:02d}. ")):
                                        try:
                                            old_f.unlink()
                                            logger.info("[QUEUE] Cleaned up superseded audio file: %s", old_f.name)
                                        except OSError:
                                            pass
                    except Exception as ce:
                        logger.debug("[QUEUE] Duplicate cleanup note: %s", ce)

                    # Trigger media-server auto-scan if configured (Phase 3: via
                    # MediaServerService — media.scan capability).
                    try:
                        from services.media_server_service import MediaServerService
                        MediaServerService().scan()
                    except Exception as ne:
                        logger.debug("[QUEUE] media auto-scan trigger note: %s", ne)

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

                    # Plugin framework (Phase 4): additive event emission for
                    # webhook/notification plugins (queue.job_failed).
                    try:
                        from plugins.manager import plugin_manager
                        if plugin_manager is not None:
                            plugin_manager.event_bus.emit(
                                "queue.job_failed",
                                job_id=job_id, track_id=track_id,
                                title=track_title, artist_name=artist_name,
                                album_name=album_name, error=combined_err,
                            )
                    except Exception:
                        logger.debug("[QUEUE] plugin job_failed event skipped", exc_info=True)

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

    # Reconcile stale jobs left in "downloading" state by an unclean shutdown
    # (container stop/restart mid-download). The worker only picks up "queued"
    # jobs, so without this they would sit in the UI as active forever.
    try:
        with app.app_context():
            stale = DownloadJob.query.filter_by(status="downloading").all()
            reset_count = 0
            for j in stale:
                j.status = "queued"
                j.progress = 0.0
                if j.track:
                    j.track.status = "queued"
                    j.track.error_message = None
                reset_count += 1
            if reset_count:
                db.session.commit()
                logger.info("[QUEUE] Reset %d stale 'downloading' job(s) to queued after unclean shutdown", reset_count)
    except Exception:
        logger.exception("[QUEUE] Failed to reconcile stale downloading jobs")

    # Purge orphaned temp work directories left behind by a killed process.
    # At boot no download is running, so every dir under /downloads/work is
    # orphaned and safe to remove; new jobs recreate their own work dirs.
    try:
        work_root = DOWNLOADS_DIR / "work"
        if work_root.exists():
            purged = 0
            for d in work_root.iterdir():
                try:
                    if d.is_dir():
                        shutil.rmtree(str(d))
                        purged += 1
                except OSError:
                    pass
            if purged:
                logger.info("[QUEUE] Purged %d orphaned temp work directorie(s) from %s", purged, work_root)
    except Exception:
        logger.exception("[QUEUE] Failed to purge orphaned temp work directories")

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


def download_manual_match_track(
    app: Flask,
    socketio: SocketIO,
    track_id: int,
    custom_url: str,
) -> Tuple[bool, str]:
    """
    Manually download, tag, and organize a track using a user-supplied Spotify, YouTube, YouTube Music, or Deezer URL.
    Overwrites/replaces any existing audio file for this track and updates database metadata.

    Runs as a SocketIO background greenlet (no inherited app context), so the
    ENTIRE body below executes inside one app context — provider invocations
    (plugin settings reads via the DB) need it. Everything is indented under
    the single `with` below, by construction.
    """
    with app.app_context():
        track = db.session.get(Track, track_id)
        if not track:
            return False, "Track not found in database"

        album = track.album
        artist = album.artist if album else None
        artist_name = artist.name if artist else "Unknown Artist"
        album_name = album.name if album else "Unknown Album"
        album_cover_url = album.cover_url if album else None
        track_title = track.title
        track_num = track.track_number or 0
        disc_num = track.disc_number or 1
        expected_duration = track.duration
        track_isrc = track.isrc
        track_external_id = track.external_id
        track_genre = track.genre or None
        # Capture plain IDs before session expiry/detachment (safe to use outside app context)
        album_id = album.id if album else 0
        artist_id = artist.id if artist else 0

        quality_setting = _get_setting(app, "spotiflac_quality", "LOSSLESS")
        fallback_format = _get_setting(app, "ytdlp_format") or _get_setting(app, "spotdl_format", "flac")
        save_cover_setting = _get_setting(app, "save_cover_art", "true").lower() != "false"
        cover_filename_setting = _get_setting(app, "cover_art_filename", "cover.jpg")
        embed_cover_setting = _get_setting(app, "embed_cover_art", "true").lower() != "false"
        cookies_path = _get_setting(app, "youtube_cookies_path", "/config/cookies.txt")
        enable_duration_check = _get_setting(app, "enable_duration_check", "true").lower() != "false"
        strictness_setting = _get_setting(app, "matching_strictness", "standard")
        max_duration_delta = STRICTNESS_DELTAS.get(strictness_setting, 8.0)

        # Mark track as downloading
        track.status = "downloading"
        track.progress = 15.0
        track.error_message = None
        db.session.commit()

        socketio.emit("download_progress", {
            "track_id": track_id,
            "album_id": album.id if album else 0,
            "artist_id": artist.id if artist else 0,
            "status": "downloading",
            "progress": 15.0,
            "title": track_title,
            "artist_name": artist_name,
        })

        music_dir = Path(_get_setting(app, "music_path", "/music"))
        dest_dir = music_dir / _sanitize(artist_name) / _sanitize(album_name)
        dest_dir.mkdir(parents=True, exist_ok=True)

        cover_bytes = _save_album_cover(dest_dir, album_cover_url, save_cover=save_cover_setting, cover_filename=cover_filename_setting)

        tmp_work_dir = DOWNLOADS_DIR / "work" / f"manual_{track_id}_{int(time.time())}"
        tmp_work_dir.mkdir(parents=True, exist_ok=True)

        target_input = custom_url.strip()
        downloaded_file: Optional[Path] = None
        last_err: Optional[str] = None
        file_meta: dict = {}

        try:
            # 1. Spotify URL
            if "open.spotify.com/track/" in target_input or target_input.startswith("spotify:track:"):
                socketio.emit("download_progress", {"track_id": track_id, "progress": 35.0, "status": "downloading"})
                ok, downloaded_file, last_err = _download_via_chain(
                    target_input,
                    tmp_work_dir,
                    quality=quality_setting,
                )
                if not ok or not downloaded_file:
                    # Fallback to yt-dlp
                    socketio.emit("download_progress", {"track_id": track_id, "progress": 60.0, "status": "downloading"})
                    ok, downloaded_file, last_err = _download_via_ytdlp_provider(
                        f"{artist_name} - {track_title}",
                        tmp_work_dir,
                        output_format=fallback_format,
                        cookies_path=cookies_path,
                        check_duration=False,
                        artist_name=artist_name,
                        track_title=track_title,
                        expected_duration=expected_duration,
                    )

            # 2. YouTube / YouTube Music URL
            elif "youtube.com" in target_input or "youtu.be" in target_input:
                socketio.emit("download_progress", {"track_id": track_id, "progress": 40.0, "status": "downloading"})
                ok, downloaded_file, last_err = _download_via_ytdlp_provider(
                    target_input,
                    tmp_work_dir,
                    output_format=fallback_format,
                    cookies_path=cookies_path,
                    check_duration=False,
                    artist_name=artist_name,
                    track_title=track_title,
                    expected_duration=expected_duration,
                )
                # Fallback if direct YouTube URL was blocked or failed: try the
                # official release on a lossless provider (Spotify resolution).
                # NOTE: no YouTube/SoundCloud *search* fallback here — the user
                # gave an explicit URL, so if it fails we must report the real
                # reason (e.g. bot-check), not silently hunt other links.
                if not ok or not downloaded_file:
                    socketio.emit("download_progress", {"track_id": track_id, "progress": 60.0, "status": "downloading"})
                    direct_err = last_err or "provided URL failed"
                    try:
                        from services.metadata_service import MetadataService
                        spot_url = MetadataService().resolve_track_url(
                            track_title, artist_name, album_name=album_name, isrc=track_isrc)
                    except Exception:
                        spot_url = None
                    if spot_url:
                        ok, downloaded_file, last_err = _download_via_chain(
                            spot_url,
                            tmp_work_dir,
                            quality=quality_setting,
                        )
                        if not downloaded_file:
                            last_err = f"Provided link failed: {direct_err} | Lossless fallback also failed: {last_err}"
                    else:
                        last_err = direct_err

            # 3. User-pasted Deezer track link (manual match; the
            #    metadata lookup below goes through the capability chain)
            elif "deezer.com/track/" in target_input:
                m = re.search(r"deezer\.com/track/(\d+)", target_input)
                if m:
                    d_id = int(m.group(1))
                    try:
                        from services.metadata_service import MetadataService
                        t_info = MetadataService().get_track_metadata(str(d_id)) or {}
                        spot_url = MetadataService().resolve_track_url(
                            t_info.get("title") or track_title,
                            t_info.get("artist_name") or artist_name,
                            isrc=t_info.get("isrc"),
                        )
                    except Exception:
                        spot_url = None
                    if spot_url:
                        ok, downloaded_file, last_err = _download_via_chain(
                            spot_url,
                            tmp_work_dir,
                            quality=quality_setting,
                        )
                    if not downloaded_file:
                        ok, downloaded_file, last_err = _download_via_ytdlp_provider(
                            f"{artist_name} - {track_title}",
                            tmp_work_dir,
                            output_format=fallback_format,
                            cookies_path=cookies_path,
                            check_duration=False,
                            artist_name=artist_name,
                            track_title=track_title,
                            expected_duration=expected_duration,
                        )
                else:
                    ok, downloaded_file, last_err = _download_via_ytdlp_provider(
                        target_input,
                        tmp_work_dir,
                        output_format=fallback_format,
                        cookies_path=cookies_path,
                        check_duration=False,
                        artist_name=artist_name,
                        track_title=track_title,
                        expected_duration=expected_duration,
                    )

            # 4. Raw Query / Other URL
            else:
                socketio.emit("download_progress", {"track_id": track_id, "progress": 40.0, "status": "downloading"})
                ok, downloaded_file, last_err = _download_via_ytdlp_provider(
                    target_input,
                    tmp_work_dir,
                    output_format=fallback_format,
                    cookies_path=cookies_path,
                    check_duration=False,
                    artist_name=artist_name,
                    track_title=track_title,
                    expected_duration=expected_duration,
                )

            if not downloaded_file or not downloaded_file.exists():
                err_msg = last_err or "Failed to download audio stream from provided URL"
                with app.app_context():
                    t = db.session.get(Track, track_id)
                    if t:
                        t.status = "failed"
                        t.progress = 0.0
                        t.error_message = err_msg
                        db.session.commit()
                socketio.emit("download_progress", {"track_id": track_id, "status": "failed", "error_message": err_msg})
                return False, err_msg

            # Verify the downloaded audio matches this track: embedded tags (artist + title)
            # are always checked so a wrong song from the provided URL is rejected; duration
            # is checked only when the duration check setting is enabled.
            v_ok, v_err, file_meta = verify_audio_file(
                downloaded_file,
                expected_duration_seconds=expected_duration if enable_duration_check else None,
                expected_artist=artist_name,
                expected_title=track_title,
                max_duration_delta=max_duration_delta,
                delete_on_failure=False,
            )
            if not v_ok:
                err_msg = (
                    f"Manual match rejected: the audio from that URL is not '{track_title}' "
                    f"by {artist_name} ({v_err}). Check the URL and try again."
                )
                logger.warning("[QUEUE] %s", err_msg)
                with app.app_context():
                    t = db.session.get(Track, track_id)
                    if t:
                        t.status = "failed"
                        t.progress = 0.0
                        t.error_message = err_msg
                        db.session.commit()
                socketio.emit("download_progress", {"track_id": track_id, "status": "failed", "error_message": err_msg})
                return False, err_msg

            with app.app_context():
                track_rec = db.session.get(Track, track_id)
                album_rec = track_rec.album if track_rec else None

                album_year = album_rec.year if album_rec else None
                total_tracks_val = album_rec.tracks.count() if album_rec else None

                ext = downloaded_file.suffix
                disc_prefix = f"{disc_num}-" if (disc_num and disc_num > 1) else ""
                track_num_prefix = f"{disc_prefix}{track_num:02d}. " if track_num else ""
                final_filename = f"{track_num_prefix}{_sanitize(track_title)}{ext}"
                final_dest = dest_dir / final_filename

                # Move into place (overwrites an existing file on POSIX; no unlink needed —
                # an explicit unlink made the folder watcher mark the track missing).
                if downloaded_file.resolve() != final_dest.resolve():
                    try:
                        shutil.move(str(downloaded_file), str(final_dest))
                    except shutil.SameFileError:
                        logger.info("[QUEUE] Work file and destination are the same file for '%s - %s'; already in place", artist_name, track_title)

                # Embed uniform tags
                _tag_audio_file(
                    final_dest,
                    artist=artist_name,
                    album=album_name,
                    title=track_title,
                    track_num=track_num,
                    year=album_year,
                    album_artist=artist_name,
                    disc_num=disc_num,
                    total_tracks=total_tracks_val,
                    cover_bytes=cover_bytes if embed_cover_setting else None,
                    genre=track_genre,
                )

                rel_path = str(final_dest.relative_to(music_dir))

                was_downloaded = bool(track_rec.is_downloaded) if track_rec else False
                if track_rec:
                    track_rec.is_downloaded = True
                    track_rec.status = "completed"
                    track_rec.progress = 100.0
                    track_rec.local_path = str(final_dest)
                    track_rec.file_path = rel_path
                    track_rec.file_format = ext.lstrip(".")
                    track_rec.size_bytes = file_meta.get("size_bytes", final_dest.stat().st_size)
                    # Keep the official expected duration as the verification baseline
                    if not track_rec.duration:
                        track_rec.duration = file_meta.get("duration") or expected_duration
                    track_rec.bitrate = file_meta.get("bitrate")
                    track_rec.error_message = None

                # Mark any active jobs completed
                active_job = DownloadJob.query.filter_by(track_id=track_id).first()
                if active_job:
                    active_job.status = "completed"
                    active_job.progress = 100.0
                    active_job.error_message = None

                # Phase 1 (scale-to-millions): guard False→True so counters only
                # move when the flag actually changes (research §2.3).
                try:
                    from services.counters_service import on_track_downloaded
                    if album_rec and album_rec.artist_id and not was_downloaded:
                        on_track_downloaded(album_rec.artist_id, is_downloaded=True)
                except Exception:
                    logger.debug("[QUEUE] counter update skipped", exc_info=True)

                if album_rec:
                    album_tracks = album_rec.tracks.all()
                    downloaded_count = sum(1 for t in album_tracks if t.is_downloaded)
                    album_rec.is_downloaded = downloaded_count == len(album_tracks)
                    album_rec.size_bytes = sum(t.size_bytes or 0 for t in album_tracks)
                    album_rec.local_path = str(dest_dir)

                db.session.commit()

            # Clean up superseded files for this track slot AFTER the DB commit so no
            # track row references the deleted file and the folder watcher stays quiet.
            try:
                for old_f in dest_dir.iterdir():
                    if old_f.is_file() and old_f.suffix.lower() in AUDIO_EXTENSIONS:
                        if old_f.resolve() != final_dest.resolve():
                            if track_num and (old_f.name.startswith(f"{track_num:02d}. ") or old_f.name.startswith(f"{disc_prefix}{track_num:02d}. ")):
                                try:
                                    old_f.unlink()
                                except OSError:
                                    pass
            except Exception:
                pass

            # Trigger media-server auto-scan (Phase 3: via MediaServerService —
            # media.scan capability).
            try:
                from services.media_server_service import MediaServerService
                MediaServerService().scan()
            except Exception:
                pass

            socketio.emit("download_progress", {
                "track_id": track_id,
                "album_id": album_id,
                "artist_id": artist_id,
                "status": "completed",
                "progress": 100.0,
                "title": track_title,
                "local_path": str(final_dest),
            })
            socketio.emit("artist_updated", {"artist_id": artist_id})

            logger.info("[QUEUE] Manual match succeeded for '%s - %s' -> %s", artist_name, track_title, final_dest)
            return True, f"Successfully downloaded and tagged '{track_title}' into library"

        except Exception as e:
            logger.exception("[QUEUE] Manual match failed for track %d: %s", track_id, e)
            with app.app_context():
                t = db.session.get(Track, track_id)
                if t:
                    t.status = "failed"
                    t.progress = 0.0
                    t.error_message = str(e)
                    db.session.commit()
            return False, str(e)

        finally:
            try:
                if tmp_work_dir.exists():
                    shutil.rmtree(str(tmp_work_dir))
            except OSError:
                pass
