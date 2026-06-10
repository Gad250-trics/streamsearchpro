import os
import re
import tempfile
import hashlib
import time
import json
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse, quote

import flask
from flask import Flask, request, jsonify, send_file, session
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
app.secret_key = "streamsearch_super_secret_key_2024"
CORS(app, supports_credentials=True)


DB_PATH = Path(tempfile.gettempdir()) / "streamsearch.db"
TEMP_DIR = Path(tempfile.gettempdir()) / "streamsearch_downloads"
FFMPEG_PATH = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_url TEXT,
            title TEXT,
            platform TEXT,
            quality TEXT,
            status TEXT,
            timestamp TEXT,
            ip TEXT,
            artist TEXT,
            artist_photo TEXT
        );
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            platform TEXT,
            results_count INTEGER,
            timestamp TEXT,
            ip TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            email TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            admin_user TEXT,
            details TEXT,
            timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            photo_url TEXT,
            channel_url TEXT,
            platform TEXT,
            source TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


# ===================== Core helpers =====================

ADMIN_CREDENTIALS = {
    "username": "admin",
    "password": "admin123",
    "email": "admin@streamsearch.com",
    "full_name": "StreamSearch Administrator",
}

LOGS_FILE = Path(tempfile.gettempdir()) / "streamsearch_logs.json"
TEMP_DIR = Path(tempfile.gettempdir()) / "streamsearch_downloads"
FFMPEG_PATH = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_old_files(older_than_seconds=3600):
    now = time.time()
    for path in TEMP_DIR.glob("*"):
        if path.is_file():
            try:
                if now - path.stat().st_mtime > older_than_seconds:
                    path.unlink()
            except Exception:
                pass


def _resolve_artist_photo(name: str, fallback_thumbnail: str, query: str) -> str:
    if fallback_thumbnail:
        return fallback_thumbnail
    try:
        encoded = quote(name)
    except Exception:
        encoded = quote(str(name), safe="")
    return f"https://ui-avatars.com/api/?name={encoded}&size=320&background=22c55e&color=ffffff&bold=true"


def detect_platform(url: str) -> str:
    lower = url.lower()
    if "youtube.com" in lower or "youtu.be" in lower:
        return "youtube"
    if "facebook.com" in lower or "fb.watch" in lower or "fb.com" in lower:
        return "facebook"
    if "tiktok.com" in lower:
        return "tiktok"
    return "unknown"


init_db()


def ffmpeg_opts() -> dict:
    return {
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }


def platform_opts(platform: str, quality: str = "720p", is_audio: bool = False) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extract_flat": False,
        "ignoreerrors": True,
    }

    lower = platform.lower()
    is_youtube = lower == "youtube"
    is_facebook = lower == "facebook"
    is_tiktok = lower == "tiktok"

    # Platform UA + cookies behavior for Facebook/TikTok
    if is_facebook or is_tiktok:
        opts["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0.0.0 Safari/537.36"
        )

    if is_audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH
        return opts

    # Video quality hints
    if is_youtube:
        if quality == "1080p":
            opts["format"] = "best[height<=1080][vcodec^=avc1]+bestaudio/best[height<=1080]"
        elif quality == "720p":
            opts["format"] = "best[height<=720][vcodec^=avc1]+bestaudio/best[height<=720]"
        elif quality == "360p":
            opts["format"] = "best[height<=360]+bestaudio/best"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
    else:
        opts["format"] = "bestvideo+bestaudio/best" if is_youtube else "best/best"

    if is_facebook:
        opts["format"] = "bestvideo*+bestaudio/best/best"
    if is_tiktok:
        opts["format"] = "(bestvideo+bestaudio)/best/best"

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts


# ===================== Diagnostics =====================

@app.route("/health", methods=["GET"])
def health_check():
    cleanup_old_files()
    return jsonify({"status": "healthy", "service": "StreamSearch Pro"})


# ===================== Download API =====================

@app.route("/api/extract/link", methods=["POST"])
def extract_video_link():
    return extract_video()

@app.route("/api/extract", methods=["POST"])
def extract_video():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not url:
        return jsonify({"success": False, "error": "URL is required"}), 400

    platform = detect_platform(url)

    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "geo_bypass": True,
        }
        if platform in ("facebook", "tiktok"):
            opts["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        thumbnail = info.get("thumbnail")
        duration = info.get("duration")
        title = info.get("title") or "Untitled"
        if not thumbnail:
            thumbnail = f"https://via.placeholder.com/640x360/000000/FFFFFF?text={platform}"
        if not duration:
            duration = 0

        return jsonify(
            {
                "success": True,
                "platform": platform,
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def download_video():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    quality = data.get("quality", "720p")
    is_audio = bool(data.get("is_audio", False))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    platform = detect_platform(url)
    file_id = hashlib.md5(f"{url}_{quality}_{is_audio}_{time.time()}".encode()).hexdigest()[:10]
    prefix = "audio" if is_audio else "video"

    out_template = str(TEMP_DIR / f"{prefix}_{file_id}_%(title)s_%(id)s.%(ext)s")
    opts = platform_opts(platform, quality=quality, is_audio=is_audio)
    opts["outtmpl"] = out_template

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)

        title = meta.get("title") or "media"
        downloaded = None
        candidates = list(TEMP_DIR.glob(f"{prefix}_{file_id}_*"))
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            downloaded = candidates[0]

        if not downloaded or not downloaded.exists():
            return jsonify({"error": "Downloaded file not found"}), 500

        return send_file(
            downloaded,
            as_attachment=True,
            download_name=downloaded.name,
            mimetype="audio/mpeg" if is_audio else "video/mp4",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/play", methods=["POST"])
def play_video():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    platform = detect_platform(url)
    file_id = hashlib.md5(f"{url}_play_{time.time()}".encode()).hexdigest()[:10]
    out_template = str(TEMP_DIR / f"play_{file_id}_%(title)s_%(id)s.%(ext)s")
    opts = platform_opts(platform, quality="720p", is_audio=False)
    opts["outtmpl"] = out_template

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)

        candidates = list(TEMP_DIR.glob(f"play_{file_id}_*"))
        if not candidates:
            return jsonify({"error": "File not found after processing"}), 500

        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        media_path = candidates[0]
        return send_file(
            media_path,
            as_attachment=False,
            mimetype="video/mp4",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/search", methods=["POST"])
def search_videos():
    data = request.get_json(silent=True) or {}
    query = data.get("query") or ""
    platform = data.get("platform") or "all"
    max_results = int(data.get("max_results") or 10)

    if not query:
        return jsonify({"success": False, "results": [], "error": "Query is required"}), 400

    results = _search_multi_platform(query, platform=platform, max_results=max_results)

    # side-effect: persist artist candidates + enrich with stored photo
    conn = db_connect()
    cur = conn.cursor()
    for item in results:
        artist = (item.get("channel") or "").strip() or "Unknown"
        thumbnail = item.get("thumbnail") or ""
        existing = cur.execute(
            "SELECT photo_url FROM artists WHERE name = ? LIMIT 1",
            (artist,),
        ).fetchone()
        if not existing:
            photo_url = _resolve_artist_photo(artist, thumbnail, query)
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO artists (name, photo_url, platform, source, updated_at) VALUES (?,?,?,?,?)",
                    (artist, photo_url, item.get("platform"), "search", datetime.utcnow().isoformat()),
                )
            except sqlite3.IntegrityError:
                pass
        else:
            photo_url = existing["photo_url"] or item.get("thumbnail")
        item["artist"] = artist
        item["artist_photo"] = photo_url or ""
    conn.commit()
    conn.close()
    return jsonify({"success": True, "results": results, "platform": platform})


def _search_multi_platform(query: str, platform: str = "all", max_results: int = 10):
    results = []
    platforms = ["youtube", "facebook", "tiktok"] if platform == "all" else [platform]

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }

    for p in platforms:
        try:
            # Only YouTube supports ytsearch stable via yt-dlp
            if p != "youtube":
                continue

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                search_query = f"ytsearch{max_results * 2}:{query}"
                info = ydl.extract_info(search_query, download=False)

            if not info:
                continue

            entries = (info.get("entries") or [])[: max_results * 2]
            for entry in entries:
                if not entry:
                    continue
                video_id = entry.get("id") or entry.get("url") or ""
                video_url = entry.get("url") or ""
                if not video_url and video_id:
                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                title = entry.get("title") or "YouTube Video"
                thumbnail = entry.get("thumbnail")
                if not thumbnail:
                    thumbnail = f"https://via.placeholder.com/640x360/FF0000/FFFFFF?text=YouTube"
                channel = entry.get("uploader") or "YouTube Channel"
                duration = entry.get("duration") or 0

                item = {
                    "id": video_id or str(abs(hash(video_url))),
                    "title": title,
                    "url": video_url,
                    "thumbnail": thumbnail,
                    "channel": channel,
                    "duration": duration,
                    "platform": "youtube",
                    "views": entry.get("view_count") or 0,
                }

                # Avoid exact dupes by URL
                if not any(r["url"] == item["url"] for r in results):
                    results.append(item)

                if len(results) >= max_results:
                    break

        except Exception as exc:
            print(f"Search error for {p}: {exc}")

    return results[:max_results]


@app.route("/api/trending", methods=["GET"])
def trending_videos():
    query = request.args.get("q") or "trending"
    platform = request.args.get("platform") or "youtube"

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "nocheckcertificate": True,
            "geo_bypass": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch15:{query}"
            info = ydl.extract_info(search_query, download=False)

        videos = []
        if info and info.get("entries"):
            for entry in info["entries"][:15]:
                if not entry:
                    continue
                vid = entry.get("id") or entry.get("url") or ""
                url = entry.get("url") or ""
                if not url and vid:
                    url = f"https://www.youtube.com/watch?v={vid}"
                thumbnail = entry.get("thumbnail") or f"https://via.placeholder.com/640x360/FF0000/FFFFFF?text=YouTube"
                videos.append(
                    {
                        "id": vid or str(abs(hash(url))),
                        "title": entry.get("title") or "Trending Video",
                        "url": url,
                        "thumbnail": thumbnail,
                        "channel": entry.get("uploader") or "YouTube",
                        "duration": entry.get("duration") or 0,
                        "platform": platform,
                        "views": entry.get("view_count") or 0,
                    }
                )
        return jsonify({"success": True, "videos": videos})
    except Exception as exc:
        return jsonify({"success": False, "results": [], "error": str(exc)}), 500


# ===================== Admin / Analytics =====================

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")

    if username == ADMIN_CREDENTIALS["username"] and password == ADMIN_CREDENTIALS["password"]:
        token = hashlib.md5(f"{username}{datetime.now().isoformat()}".encode()).hexdigest()
        session["admin_token"] = token
        session["admin_user"] = username
        return jsonify(
            {
                "success": True,
                "token": token,
                "user": {
                    "username": ADMIN_CREDENTIALS["username"],
                    "email": ADMIN_CREDENTIALS["email"],
                    "full_name": ADMIN_CREDENTIALS["full_name"],
                },
            }
        )
    return jsonify({"success": False, "error": "Invalid credentials"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out"})


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    logs = _load_logs()
    downloads = logs.get("downloads", [])
    searches = logs.get("searches", [])

    today = datetime.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    downloads_today = sum(1 for d in downloads if datetime.fromisoformat(d["timestamp"]).date() == today)
    downloads_week = sum(1 for d in downloads if datetime.fromisoformat(d["timestamp"]).date() >= week_ago)
    downloads_month = sum(1 for d in downloads if datetime.fromisoformat(d["timestamp"]).date() >= month_ago)

    total_downloads = len(downloads)
    total_searches = len(searches)

    platform_stats = {}
    quality_stats = {}
    for d in downloads:
        platform_stats[d.get("platform", "unknown")] = platform_stats.get(d.get("platform", "unknown"), 0) + 1
        quality_stats[d.get("quality", "unknown")] = quality_stats.get(d.get("quality", "unknown"), 0) + 1

    daily_downloads = []
    for i in range(6, -1, -1):
        date = today - timedelta(days=i)
        c = sum(
            1
            for d in downloads
            if datetime.fromisoformat(d["timestamp"]).date() == date
        )
        daily_downloads.append({"date": date.strftime("%Y-%m-%d"), "count": c})

    recent_downloads = downloads[:10]

    search_queries = {}
    for s in searches:
        q = (s.get("query") or "").lower().strip()
        if q:
            search_queries[q] = search_queries.get(q, 0) + 1
    popular_searches = sorted(search_queries.items(), key=lambda x: x[1], reverse=True)[:10]

    last_week_count = sum(d["count"] for d in daily_downloads)
    prev_week_start = today - timedelta(days=14)
    prev_week_end = today - timedelta(days=7)
    previous_week_downloads = sum(
        1
        for d in downloads
        if prev_week_start <= datetime.fromisoformat(d["timestamp"]).date() <= prev_week_end
    )
    trend = round(((last_week_count - previous_week_downloads) / (previous_week_downloads or 1)) * 100, 1)

    return jsonify(
        {
            "success": True,
            "stats": {
                "downloads_today": downloads_today,
                "downloads_week": downloads_week,
                "downloads_month": downloads_month,
                "total_downloads": total_downloads,
                "total_searches": total_searches,
                "platform_stats": platform_stats,
                "quality_stats": quality_stats,
                "daily_downloads": daily_downloads,
                "recent_downloads": recent_downloads,
                "popular_searches": popular_searches,
                "trend_percentage": trend,
            },
        }
    )


# ===================== Logging =====================

def _load_logs() -> dict:
    if not LOGS_FILE.exists():
        return {"downloads": [], "searches": [], "users": [], "admin_actions": []}
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"downloads": [], "searches": [], "users": [], "admin_actions": []}


def _save_logs(logs: dict) -> None:
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


# ============================================================
# Cleanup endpoint used by existing UI
# ============================================================
@app.route("/api/cleanup", methods=["POST"])
def cleanup_downloads():
    try:
        for path in TEMP_DIR.glob("*"):
            if path.is_file():
                path.unlink()
        return jsonify({"success": True, "message": "Cache cleaned successfully"})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/stream", methods=["POST"])
def stream_video():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    platform = detect_platform(url)
    file_id = hashlib.md5(f"{url}_stream_{time.time()}".encode()).hexdigest()[:10]
    out_template = str(TEMP_DIR / f"stream_{file_id}_%(title)s_%(id)s.%(ext)s")
    opts = platform_opts(platform, quality="720p", is_audio=False)
    opts["outtmpl"] = out_template

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)

        candidates = list(TEMP_DIR.glob(f"stream_{file_id}_*"))
        if not candidates:
            return jsonify({"error": "File not found after processing"}), 500

        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        media_path = candidates[0]
        return send_file(
            media_path,
            as_attachment=False,
            mimetype="video/mp4",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pest/stats", methods=["GET"])
def pest_stats():
    cache_files = len(list(TEMP_DIR.glob("*")))
    cache_size = sum(f.stat().st_size for f in TEMP_DIR.glob("*") if f.is_file())
    return jsonify({
        "cache_files": cache_files,
        "cache_size_mb": round(cache_size / 1024 / 1024, 2)
    })


@app.route("/api/download/batch", methods=["POST"])
def batch_download():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    quality = data.get("quality", "720p")

    if not urls:
        return jsonify({"success": False, "error": "No URLs provided"}), 400

    results = []
    for url in urls:
        try:
            platform = detect_platform(url)
            file_id = hashlib.md5(f"{url}_{quality}_{time.time()}".encode()).hexdigest()[:10]
            out_template = str(TEMP_DIR / f"batch_{file_id}_%(title)s_%(id)s.%(ext)s")
            opts = platform_opts(platform, quality=quality, is_audio=False)
            opts["outtmpl"] = out_template

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            candidates = list(TEMP_DIR.glob(f"batch_{file_id}_*"))
            downloaded = candidates[0] if candidates else None

            results.append({"url": url, "status": "success" if downloaded else "failed"})
        except Exception as exc:
            results.append({"url": url, "status": "failed", "error": str(exc)})

    return jsonify({"success": True, "results": results})


if __name__ == "__main__":
    print("==================================================")
    print(" StreamSearch Pro – Video Downloader Backend")
    print("==================================================")
    print(f"Temp directory  : {TEMP_DIR}")
    print(f"Logs file       : {LOGS_FILE}")
    print(f"Server          : http://localhost:5000")
    print(f"Open index.html : {Path('index.html').resolve()}")
    print("==================================================")
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
