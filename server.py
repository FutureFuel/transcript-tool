from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from fastmcp import FastMCP
import yt_dlp

# Auto-install optional dependencies that must be present regardless of which
# Python environment launches the server.
def _ensure_pkg(import_name: str, pip_spec: str) -> None:
    try:
        __import__(import_name)
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", pip_spec],
            stdout=subprocess.DEVNULL,
        )

_ensure_pkg("curl_cffi", "curl-cffi>=0.10,<0.16")
_ensure_pkg("youtube_transcript_api", "youtube-transcript-api>=0.6")


def _resolve_cookies_file() -> None:
    """
    If YTDLP_COOKIES_FILE is an https:// or s3:// URL, download it to a
    local temp file and update the env var to that path.
    Supports both private S3 buckets (via boto3 IAM auth) and public HTTPS URLs.
    Runs once at startup so every tool picks up the local path transparently.
    """
    value = os.environ.get("YTDLP_COOKIES_FILE", "")
    if not value:
        print("[server] YTDLP_COOKIES_FILE not set — YouTube requests may be blocked by IP",
              flush=True)
        return
    if Path(value).is_file():
        size = Path(value).stat().st_size
        print(f"[server] cookies loaded from local path: {value} ({size} bytes)", flush=True)
        return

    local_path = Path(tempfile.gettempdir()) / "ytdlp_cookies.txt"

    # Parse S3 URLs: both s3:// and https://*.s3.*.amazonaws.com/* formats
    s3_bucket: str | None = None
    s3_key: str | None = None

    if value.startswith("s3://"):
        parts = value[5:].split("/", 1)
        s3_bucket, s3_key = parts[0], (parts[1] if len(parts) > 1 else "")
    elif "s3." in value and "amazonaws.com" in value:
        # e.g. https://bucket.s3.region.amazonaws.com/key
        import urllib.parse as _up
        parsed = _up.urlparse(value)
        host_parts = parsed.netloc.split(".")
        s3_bucket = host_parts[0]
        s3_key = parsed.path.lstrip("/")

    if s3_bucket and s3_key:
        try:
            import boto3 as _boto3
            region = os.environ.get("AWS_REGION", "us-east-1")
            _boto3.client("s3", region_name=region).download_file(
                s3_bucket, s3_key, str(local_path)
            )
            size = local_path.stat().st_size
            os.environ["YTDLP_COOKIES_FILE"] = str(local_path)
            print(f"[server] cookies downloaded from S3 s3://{s3_bucket}/{s3_key} "
                  f"→ {local_path} ({size} bytes)", flush=True)
            return
        except Exception as exc:
            print(f"[server] WARNING: S3 cookies download failed: {exc}", flush=True)

    if value.startswith("https://") or value.startswith("http://"):
        try:
            req = urllib.request.Request(value, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            local_path.write_bytes(data)
            os.environ["YTDLP_COOKIES_FILE"] = str(local_path)
            print(f"[server] cookies downloaded from URL → {local_path} ({len(data)} bytes)",
                  flush=True)
            return
        except Exception as exc:
            print(f"[server] WARNING: HTTPS cookies download failed: {exc}", flush=True)

    print(f"[server] WARNING: YTDLP_COOKIES_FILE='{value}' is not a valid path or URL — "
          "cookies will not be used", flush=True)


_resolve_cookies_file()

_FFMPEG_BUILDS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-linux64-gpl.tar.xz"
)
_FFMPEG_CACHE: str | None = None
_FFMPEG_LOCK = __import__("threading").Lock()


def _disk_free_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Startup: clean up any stale yt-dlp temp files and check available disk space.
# Previous failed runs may have left .part files, subtitles, or info-json files
# in /tmp. Purge them now so the container starts with a clean slate.
# ---------------------------------------------------------------------------
def _startup_cleanup() -> None:
    import glob as _glob

    patterns = [
        "/tmp/ytdlp-*",          # our own outtmpl prefix (legacy)
        "/tmp/*.part",           # yt-dlp partial downloads
        "/tmp/*.vtt",            # subtitle files
        "/tmp/*.json3",          # json3 subtitle files
        "/tmp/*.info.json",      # yt-dlp info dumps
        "/tmp/*.description",    # yt-dlp description files
        "/tmp/*.annotations.xml",
        "/tmp/tmp*.mp3",
        "/tmp/tmp*.mp4",
        "/tmp/tmp*.m4a",
        "/tmp/tmp*.webm",
    ]
    removed = 0
    for pattern in patterns:
        for f in _glob.glob(pattern):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    free_mb = _disk_free_mb(Path("/tmp"))
    if removed:
        print(f"[startup] cleaned {removed} stale temp file(s); /tmp free: {free_mb:.0f} MB", flush=True)
    if free_mb < 200:
        print(f"[startup] WARNING: only {free_mb:.0f} MB free on /tmp — "
              "audio transcription may fail with [Errno 28]", flush=True)


_startup_cleanup()

import s3_utils

mcp = FastMCP("yt-dlp")


def _resolve_ffmpeg_location() -> str | None:
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE or None

    with _FFMPEG_LOCK:
        # Re-check inside the lock — another thread may have resolved it.
        if _FFMPEG_CACHE is not None:
            return _FFMPEG_CACHE or None

        env_location = os.environ.get("FFMPEG_LOCATION")
        if env_location:
            _FFMPEG_CACHE = env_location
            return env_location

        # imageio-ffmpeg ships a bundled ffmpeg binary — try it first so we
        # never need a system install or a GitHub download on managed hosts.
        # NOTE: its binary is named e.g. "ffmpeg-linux64-v7.1", not just "ffmpeg",
        # so we store the FULL PATH to the executable, not the directory.
        try:
            import imageio_ffmpeg as _iio_ffmpeg
            _iio_bin = _iio_ffmpeg.get_ffmpeg_exe()
            if _iio_bin and Path(_iio_bin).is_file():
                _FFMPEG_CACHE = str(_iio_bin)
                print(f"[ffmpeg] using imageio-ffmpeg binary: {_iio_bin}", flush=True)
                return _FFMPEG_CACHE
        except Exception:
            pass

        # Check common explicit paths in case PATH is restricted.
        for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
            if Path(candidate).is_file():
                _FFMPEG_CACHE = candidate
                return _FFMPEG_CACHE

        found = shutil.which("ffmpeg")
        if found and shutil.which("ffprobe"):
            _FFMPEG_CACHE = found
            return _FFMPEG_CACHE

        cache_dir = Path(os.environ.get("YTDLP_FFMPEG_DIR", "/tmp/yt-dlp-ffmpeg"))
        ffmpeg_bin = cache_dir / "ffmpeg"
        ffprobe_bin = cache_dir / "ffprobe"
        if ffmpeg_bin.is_file() and ffprobe_bin.is_file():
            _FFMPEG_CACHE = str(ffmpeg_bin)
            return _FFMPEG_CACHE

        system = platform.system().lower()
        machine = platform.machine().lower()
        if system != "linux" or machine not in {"x86_64", "amd64"}:
            _FFMPEG_CACHE = ""
            return None

        free_mb = _disk_free_mb(cache_dir if cache_dir.exists() else cache_dir.parent)
        if free_mb < 300:
            print(f"[ffmpeg] only {free_mb:.0f} MB free — skipping download to avoid ENOSPC", flush=True)
            _FFMPEG_CACHE = ""
            return None

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "ffmpeg.tar.xz"
                with urllib.request.urlopen(_FFMPEG_BUILDS_URL, timeout=120) as response:
                    archive.write_bytes(response.read())
                with tarfile.open(archive, "r:xz") as tar:
                    tar.extractall(tmp, filter="data")
                build_root = next(Path(tmp).glob("ffmpeg-*-linux64-gpl"))
                for name, dest in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
                    shutil.copy2(build_root / "bin" / name, dest)
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            _FFMPEG_CACHE = str(ffmpeg_bin)
            return _FFMPEG_CACHE
        except Exception as exc:
            # Mark as resolved-but-unavailable so we never retry and never fill /tmp again.
            print(f"[ffmpeg] download failed ({exc}) — audio transcription will be unavailable", flush=True)
            _FFMPEG_CACHE = ""
            return None


def make_ydl_opts(*, require_ffmpeg: bool = False, **options) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "logger": _SilentLogger(),
        **options,
        # These are applied AFTER **options so no caller can re-enable disk writes.
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
        "writethumbnail": False,
        "writedescription": False,
        "writeannotations": False,
        "noprogress": True,
    }
    if require_ffmpeg:
        ffmpeg_bin_path = _resolve_ffmpeg_location()
        if ffmpeg_bin_path:
            # yt-dlp expects the directory containing the binary, not the binary itself.
            opts.setdefault("ffmpeg_location", str(Path(ffmpeg_bin_path).parent))
    # Support a Netscape-format cookies file for age-restricted / auth-gated videos.
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).is_file():
        opts.setdefault("cookiefile", cookies_file)
    # NOTE: proxy is intentionally NOT added here globally.
    # It is only injected for YouTube-specific calls inside _fetch_youtube_transcript
    # so that Dailymotion, podcast, and other non-YouTube yt-dlp calls are unaffected.
    opts.setdefault("age_limit", 99)
    opts.setdefault("extractor_args", {"youtube": {"player_client": ["ios", "web"]}})
    return opts


_BOT_CHECK_HINTS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "bot",
    "login required",
    "authentication",
    "members only",
    "private",
    "age-restricted",
    "confirm your age",
)


def _is_auth_error(msg: str) -> bool:
    return any(h in msg.lower() for h in _BOT_CHECK_HINTS)


class _SilentLogger:
    """Suppress all yt-dlp console output — errors are handled via exceptions."""
    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass


def _youtube_oembed_meta(video_id: str) -> dict:
    """
    Fetch lightweight YouTube metadata via the oEmbed endpoint.
    Requires no authentication and is never bot-checked.
    """
    import json as _json
    oembed_url = (
        f"https://www.youtube.com/oembed"
        f"?url=https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        with urllib.request.urlopen(oembed_url, timeout=10) as resp:
            data = _json.loads(resp.read())
        return {
            "title": data.get("title"),
            "uploader": data.get("author_name"),
            "duration_seconds": None,
            "upload_date": None,
        }
    except Exception:
        return {}


def _youtube_video_id(url: str) -> str | None:
    """Extract the YouTube video ID from any common URL format."""
    import urllib.parse as _up
    parsed = _up.urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0].split("?")[0] or None
    if host == "youtube.com":
        if parsed.path.startswith("/watch"):
            return _up.parse_qs(parsed.query).get("v", [None])[0]
        for prefix in ("/shorts/", "/live/", "/embed/", "/v/"):
            if parsed.path.startswith(prefix):
                return parsed.path[len(prefix):].split("/")[0].split("?")[0] or None
    return None


_INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yt.cdaut.de",
    "https://invidious.privacydev.net",
    "https://invidious.perennialte.ch",
]


def _fetch_transcript_via_invidious(
    video_id: str, language: str, session=None
) -> tuple[str | None, str | None]:
    """
    Fetch YouTube captions via public Invidious instances.
    Accepts an optional requests.Session so proxy/cookie settings are honoured.
    """
    import json as _json
    import re as _re

    if session is None:
        import requests as _req
        session = _req.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

    last_error: str = "no instances tried"
    for instance in _INVIDIOUS_INSTANCES:
        try:
            resp = session.get(f"{instance}/api/v1/captions/{video_id}", timeout=10)
            resp.raise_for_status()
            data = _json.loads(resp.content)

            captions = data.get("captions", [])
            if not captions:
                last_error = f"{instance}: no captions returned"
                continue

            # Invidious returns camelCase 'languageCode'
            def _priority(cap: dict) -> int:
                code = cap.get("languageCode", "").lower()
                label = cap.get("label", "").lower()
                if code == language or code.startswith(f"{language}-"):
                    return 0 if "auto" not in label else 1
                return 2

            captions.sort(key=_priority)
            cap_path = captions[0].get("url", "")
            cap_url = f"{instance}{cap_path}" if cap_path.startswith("/") else cap_path
            sep = "&" if "?" in cap_url else "?"
            cap_url += f"{sep}format=vtt"

            resp2 = session.get(cap_url, timeout=15)
            resp2.raise_for_status()
            vtt = resp2.text

            lines = []
            for line in vtt.splitlines():
                line = line.strip()
                if not line or "-->" in line or line.upper().startswith("WEBVTT") or line.isdigit():
                    continue
                line = _re.sub(r"<[^>]+>", "", line)
                if line:
                    lines.append(line)

            text = " ".join(lines).strip()
            if text:
                return text, None
            last_error = f"{instance}: empty transcript after parsing"
        except Exception as exc:
            last_error = f"{instance}: {type(exc).__name__}: {exc}"

    return None, f"All Invidious instances failed. Last: {last_error}"


def _fetch_transcript_via_supadata(video_id: str, language: str) -> tuple[str | None, str | None]:
    """
    Fetch YouTube transcript via the Supadata API (https://supadata.ai).
    Supadata runs its own infrastructure so cloud/datacenter IP blocks don't apply.

    Requires env var: SUPADATA_API_KEY  (free tier: 100 req/month at dash.supadata.ai)

    Returns (text, error).
    """
    api_key = os.environ.get("SUPADATA_API_KEY")
    if not api_key:
        return None, "SUPADATA_API_KEY not set"

    import json as _json
    import urllib.parse as _parse

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    params = _parse.urlencode({"url": video_url, "lang": language, "text": "true", "mode": "auto"})
    req = urllib.request.Request(
        f"https://api.supadata.ai/v1/transcript?{params}",
        headers={"x-api-key": api_key, "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())

        # text=true → response has a top-level "content" string
        text = data.get("content", "")
        if isinstance(text, str) and text.strip():
            return text.strip(), None

        # text=false fallback: join segment texts
        segments = data.get("chunks") or data.get("segments") or []
        if segments:
            text = " ".join(s.get("text", "") for s in segments).strip()
            if text:
                return text, None

        return None, f"Supadata returned empty content: {list(data.keys())}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        return None, f"Supadata HTTP {exc.code}: {body}"
    except Exception as exc:
        return None, f"Supadata error: {type(exc).__name__}: {exc}"


def _parse_json3_transcript(data: dict) -> str:
    """Parse YouTube json3 timed-text format into plain text."""
    texts: list[str] = []
    for ev in data.get("events", []):
        for seg in ev.get("segs", []):
            t = seg.get("utf8", "").strip()
            if t and t != "\n":
                texts.append(t)
    return " ".join(texts).replace("  ", " ").strip()


def _fetch_youtube_transcript(video_id: str, language: str) -> tuple[str | None, str | None]:
    """
    Fetch YouTube transcript, trying every free method before spending API credits.

    Order (cheapest / most reliable first):
    1. yt-dlp iOS client + direct timedtext fetch  — free, no rate limit
    2. Invidious public API                         — free, no rate limit
    3. youtube-transcript-api (cookie session)      — free, may be IP-blocked
    4. Supadata API (LAST RESORT)                   — 100 free req/month;
       only used when all three free methods fail.
       Set SUPADATA_API_KEY to enable (dash.supadata.ai).

    Returns (text, error).
    """
    # ------------------------------------------------------------------
    # Build a cookie-authenticated session used for ALL HTTP fetches below.
    # YouTube's timedtext CDN and the youtube-transcript-api both need the
    # same session cookies to accept requests from datacenter IPs.
    # ------------------------------------------------------------------
    import http.cookiejar as _cookiejar
    import requests as _requests

    _session = _requests.Session()
    _session.headers.update({"User-Agent": "Mozilla/5.0"})
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies_file and Path(cookies_file).is_file():
        _jar = _cookiejar.MozillaCookieJar(cookies_file)
        try:
            _jar.load(ignore_discard=True, ignore_expires=True)
            _session.cookies = _jar  # type: ignore[assignment]
        except Exception:
            pass
    _proxy = (
        os.environ.get("YTDLP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    if _proxy:
        _session.proxies.update({"http": _proxy, "https": _proxy})

    # ------------------------------------------------------------------
    # Step 1: yt-dlp iOS client → timedtext URL → authenticated HTTP fetch
    # process=False skips process_video_result (which errors on missing
    # video formats) so we only extract caption metadata, not streams.
    # The timedtext CDN requires the same YouTube session cookies on AWS IPs.
    # ------------------------------------------------------------------
    fetch_errors: list[str] = []
    try:
        ydl_opts = make_ydl_opts(
            extractor_args={"youtube": {"player_client": ["ios"]}},
            no_warnings=True,
        )
        # Apply proxy only for YouTube — other platforms don't need it and
        # routing them through a datacenter proxy causes more problems than it solves.
        if _proxy:
            ydl_opts["proxy"] = _proxy
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
                process=False,
            )

        auto_caps: dict = info.get("automatic_captions", {})
        manual_subs: dict = info.get("subtitles", {})
        cookies_loaded = cookies_file and Path(cookies_file).is_file()
        print(
            f"[yt-dlp timedtext] video={video_id} lang={language} "
            f"auto_langs={len(auto_caps)} manual_langs={len(manual_subs)} "
            f"cookies={'yes' if cookies_loaded else 'no'} proxy={'yes' if _proxy else 'no'}",
            flush=True,
        )

        def _find_entries(caps_dict: dict) -> list[dict]:
            for code in [language, f"{language}-orig", language.split("-")[0]]:
                if code in caps_dict:
                    return caps_dict[code]
            return []

        entries = _find_entries(manual_subs) or _find_entries(auto_caps)
        if not entries:
            entries = next(iter(auto_caps.values()), [])

        pref = {"json3": 0, "srv3": 1, "srv2": 2, "srv1": 3, "vtt": 4}
        entries_sorted = sorted(entries, key=lambda e: pref.get(e.get("ext", ""), 99))

        for entry in entries_sorted:
            sub_url = entry.get("url")
            if not sub_url:
                continue
            try:
                resp = _session.get(sub_url, timeout=15)
                resp.raise_for_status()
                raw = resp.content

                ext = entry.get("ext", "")
                if ext == "json3":
                    import json as _json

                    text = _parse_json3_transcript(_json.loads(raw))
                else:
                    import re as _re

                    lines: list[str] = []
                    for line in raw.decode("utf-8").splitlines():
                        line = line.strip()
                        if not line or "-->" in line or line.upper().startswith("WEBVTT"):
                            continue
                        if line.isdigit():
                            continue
                        line = _re.sub(r"<[^>]+>", "", line)
                        if line:
                            lines.append(line)
                    text = " ".join(lines).strip()

                if text:
                    return text, None
                fetch_errors.append(f"{ext}: empty content")
            except Exception as fetch_exc:
                fetch_errors.append(f"{entry.get('ext','?')}: {type(fetch_exc).__name__}({fetch_exc})")
                continue
    except Exception as ytdlp_exc:
        ytdlp_error = f"yt-dlp extract_info: {type(ytdlp_exc).__name__}: {ytdlp_exc}"
    else:
        errs = "; ".join(fetch_errors) if fetch_errors else "no subtitle entries returned"
        ytdlp_error = f"yt-dlp timedtext fetch failed: {errs}"

    # ------------------------------------------------------------------
    # Step 2: Invidious public API fallback (uses same proxy session)
    # ------------------------------------------------------------------
    text, inv_error = _fetch_transcript_via_invidious(video_id, language, session=_session)
    if text:
        return text, None

    # ------------------------------------------------------------------
    # Step 3: youtube-transcript-api reusing the same cookie session
    # ------------------------------------------------------------------
    try:
        from youtube_transcript_api import (
            YouTubeTranscriptApi,
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
            RequestBlocked,
            IpBlocked,
        )
    except ImportError:
        return None, f"{ytdlp_error}; Invidious: {inv_error}; youtube-transcript-api not installed"

    try:
        api = YouTubeTranscriptApi(http_client=_session)
        for langs in ([language], None):
            try:
                fetch_kwargs: dict = {"languages": langs} if langs else {}
                transcript = api.fetch(video_id, **fetch_kwargs)
                text = " ".join(s.text for s in transcript).strip()
                return text, None
            except NoTranscriptFound:
                if langs is None:
                    return None, (
                        f"No transcript available for video {video_id} in any language. "
                        f"yt-dlp: {ytdlp_error}; Invidious: {inv_error}"
                    )
        return None, "No transcript found"
    except (RequestBlocked, IpBlocked) as exc:
        api_error = f"{type(exc).__name__}"
    except TranscriptsDisabled:
        return None, "Transcripts are disabled for this video"
    except VideoUnavailable:
        return None, "Video is unavailable"
    except Exception as exc:
        api_error = f"{type(exc).__name__}: {exc}"
    else:
        api_error = "no transcript found"

    # ------------------------------------------------------------------
    # Step 4: Supadata API — last resort; spends from the monthly quota.
    # Only reached when all three free methods above have failed.
    # ------------------------------------------------------------------
    text, supa_error = _fetch_transcript_via_supadata(video_id, language)
    if text:
        return text, None

    return None, (
        f"All methods failed. "
        f"yt-dlp: {ytdlp_error}; "
        f"Invidious: {inv_error}; "
        f"youtube-transcript-api: {api_error}; "
        f"Supadata: {supa_error}"
    )


def _extract_info_with_fallback(url: str, ydl_opts: dict) -> dict:
    """
    Try extract_info with the given opts; if YouTube bot-check fires, retry
    with the TV-embedded client which bypasses the check without cookies.
    """
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.ExtractorError as exc:
        if not _is_auth_error(str(exc)):
            raise
        # Bot check hit — retry with tv_embedded which has no bot verification
        fallback_opts = {
            **ydl_opts,
            "extractor_args": {"youtube": {"player_client": ["tv_embedded", "ios"]}},
        }
        with yt_dlp.YoutubeDL(fallback_opts) as ydl:
            return ydl.extract_info(url, download=False)


# ─────────────────────────────────────────────
# Workstream B: Ingestion & Normalization
# ─────────────────────────────────────────────

@mcp.tool()
def get_video_transcript(url: str, language: str = "en") -> dict:
    """
    Download a video and extract its transcript/subtitles for filtering
    and summarization. Works for YouTube (native captions) and any other
    yt-dlp supported source including Dailymotion (falls back to AWS Transcribe).
    Returns transcript text, title, uploader, and duration.
    """
    # No writesubtitles/writeautomaticsub — those cause disk writes even with
    # skip_download=True.  Caption URLs come from the info dict instead.
    ydl_opts = make_ydl_opts(skip_download=True)
    try:
        info = _extract_info_with_fallback(url, ydl_opts)
    except Exception as exc:
        return {
            "title": None, "uploader": None, "duration_seconds": None,
            "upload_date": None, "url": url,
            "has_transcript": False, "transcript_url": None,
            "transcript_source": "failed",
            "error": str(exc)[:400],
        }
    subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})
    transcript_url = None
    if language in subtitles:
        for fmt in subtitles[language]:
            if fmt.get("ext") in ("vtt", "srv3", "json3"):
                transcript_url = fmt.get("url")
                break

    if transcript_url is not None:
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "url": url,
            "transcript_url": transcript_url,
            "has_transcript": True,
            "transcript_source": "native_subtitles",
        }

    # No native subtitles — fall back to AWS Transcribe for audio-based sources
    # (e.g. Dailymotion, Vimeo, and other platforms without captions).
    language_code_map = {
        "en": "en-US", "fr": "fr-FR", "de": "de-DE", "es": "es-ES",
        "it": "it-IT", "pt": "pt-BR", "nl": "nl-NL", "ja": "ja-JP",
        "ko": "ko-KR", "zh": "zh-CN",
    }
    language_code = language_code_map.get(language, f"{language}-{language.upper()}")
    transcription = start_podcast_transcription(url, language_code=language_code)

    if transcription.get("status") == "transcription_started":
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "url": url,
            "has_transcript": False,
            "transcript_url": None,
            "transcript_source": "aws_transcribe_async",
            "transcription_job_name": transcription.get("job_name"),
            "next_step": f"Call get_transcription_result('{transcription.get('job_name')}') in 2-5 minutes to fetch the transcript.",
        }

    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "url": url,
        "has_transcript": False,
        "transcript_url": None,
        "transcript_source": "none",
        "error": transcription.get("error", "No subtitles available and transcription could not be started."),
    }


@mcp.tool()
def get_podcast_stream_url(url: str) -> dict:
    """
    Resolve the direct audio stream URL for a podcast episode without downloading
    or writing any files to disk. Zero disk space used.
    Pass the returned stream_url directly to a transcription service (e.g. Whisper API).
    Use this instead of get_podcast_transcript whenever disk space is limited.
    Returns: stream_url, title, uploader, duration_seconds, upload_date, description.
    """
    ydl_opts = make_ydl_opts(format="bestaudio/best")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats") or []
        # Pick the best audio-only format with a direct URL
        audio_formats = [
            f for f in formats
            if f.get("url") and f.get("acodec") != "none"
            and f.get("vcodec") in (None, "none", "")
        ]
        best = audio_formats[-1] if audio_formats else (formats[-1] if formats else {})
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration_seconds": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "description": (info.get("description") or "")[:500],
            "stream_url": best.get("url"),
            "audio_ext": best.get("ext"),
            "filesize_bytes": best.get("filesize") or best.get("filesize_approx"),
            "source_url": url,
            "disk_usage": "none — stream directly from stream_url, do not download",
        }


@mcp.tool()
def get_podcast_transcript(url: str, audio_format: str = "mp3") -> dict:
    """
    Extract audio from a podcast episode, stream it directly to S3 via ffmpeg
    (zero local disk usage), and return the S3 location for downstream transcription.
    Requires the S3_BUCKET environment variable and AWS credentials.
    Returns s3_uri, s3_download_url, and episode metadata.
    """
    # Delegate entirely to start_podcast_transcription which already uses the
    # zero-disk ffmpeg→pipe→S3 pipeline.
    result = start_podcast_transcription(url)
    if result.get("error"):
        return result
    return {
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "duration_seconds": result.get("duration_seconds"),
        "upload_date": result.get("upload_date"),
        "s3_uri": f"s3://{s3_utils.bucket_name()}/{result.get('transcript_s3_key', '')}",
        "s3_download_url": s3_utils.presigned_download_url(result.get("transcript_s3_key", "")),
        "s3_key": result.get("transcript_s3_key"),
        "job_name": result.get("job_name"),
        "url": url,
    }


@mcp.tool()
def get_source_metadata(url: str) -> dict:
    """
    Extract metadata from any yt-dlp supported source without downloading.
    Used to validate a new source before adding it to the approved source list.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "duration_seconds": info.get("duration"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "description": (info.get("description") or "")[:800],
            "webpage_url": info.get("webpage_url"),
            "extractor": info.get("extractor"),
            "is_live": info.get("is_live", False),
            "availability": info.get("availability"),
        }


@mcp.tool()
def get_playlist_items(playlist_url: str, max_items: int = 20) -> list:
    """
    Fetch all video/episode entries from a YouTube playlist or channel.
    Used to monitor video sources for new content during the daily ingestion refresh.
    """
    ydl_opts = make_ydl_opts(extract_flat=True, playlistend=max_items)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        entries = info.get("entries", [])
        return [
            {
                "title": e.get("title"),
                "url": e.get("url") or e.get("webpage_url"),
                "upload_date": e.get("upload_date"),
                "duration_seconds": e.get("duration"),
                "id": e.get("id"),
            }
            for e in entries if e
        ]


@mcp.tool()
def check_transcript_quality(url: str) -> dict:
    """
    Check whether a video/podcast source has a usable transcript.
    Returns quality indicators to decide whether to ingest or flag for admin review.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        subtitles = info.get("subtitles", {})
        auto_captions = info.get("automatic_captions", {})
        has_manual = bool(subtitles)
        has_auto = bool(auto_captions)
        return {
            "title": info.get("title"),
            "has_manual_transcript": has_manual,
            "has_auto_transcript": has_auto,
            "has_any_transcript": has_manual or has_auto,
            "manual_transcript_languages": list(subtitles.keys()),
            "auto_transcript_languages": list(auto_captions.keys()),
            "recommended_action": (
                "ingest" if has_manual
                else "ingest_with_caution" if has_auto
                else "flag_for_admin"
            ),
            "duration_seconds": info.get("duration"),
        }


# ─────────────────────────────────────────────
# Workstream A: Source List & Schema support
# ─────────────────────────────────────────────

@mcp.tool()
def validate_source(url: str) -> dict:
    """
    Validate a candidate source URL before adding it to the approved source list.
    Checks reachability, source type, and transcript availability.
    """
    ydl_opts = make_ydl_opts(extract_flat=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            extractor = info.get("extractor", "unknown")
            is_playlist = info.get("_type") == "playlist"
            entries = info.get("entries", [])
            return {
                "url": url,
                "reachable": True,
                "source_type": (
                    "playlist_or_channel" if is_playlist
                    else "single_video" if extractor in ("youtube", "vimeo")
                    else "podcast_or_audio" if extractor in ("soundcloud", "buzzsprout", "simplecast")
                    else "article_or_rss"
                ),
                "extractor": extractor,
                "title": info.get("title"),
                "uploader": info.get("uploader"),
                "item_count": len(entries) if is_playlist else 1,
                "suggested_schema_fields": {
                    "source_link": url,
                    "monitoring_contexts": [],
                    "why_it_matters": "",
                    "cadence": info.get("upload_frequency", "unknown"),
                    "last_successful_fetch": None,
                    "error_state": None,
                },
            }
    except Exception as e:
        return {
            "url": url,
            "reachable": False,
            "error": str(e),
            "recommended_action": "do_not_add",
        }


@mcp.tool()
def list_formats(url: str) -> list:
    """
    List all available media formats for a given URL.
    Useful for determining the best ingestion format per source type.
    """
    with yt_dlp.YoutubeDL(make_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        return [
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "filesize": f.get("filesize"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "tbr": f.get("tbr"),
            }
            for f in formats
        ]


# ─────────────────────────────────────────────
# Workstream C: Filtering, Summarization & Slack Output
# ─────────────────────────────────────────────

@mcp.tool()
def keyword_filter(
    text: str,
    keywords: list[str],
    monitoring_context: str = "",
) -> dict:
    """
    Phase 1 of the two-pass filtering strategy: keyword match.
    Scans transcript/article text for any of the provided keywords.
    Per project spec: always run LLM pass even if no keywords match.
    """
    text_lower = text.lower()
    matches = {}
    for kw in keywords:
        pattern = re.compile(re.escape(kw.lower()))
        found = pattern.findall(text_lower)
        if found:
            matches[kw] = len(found)

    return {
        "keyword_matches": matches,
        "match_count": sum(matches.values()),
        "matched_keywords": list(matches.keys()),
        "has_keyword_match": bool(matches),
        "monitoring_context": monitoring_context,
        "recommendation": (
            "proceed_to_llm_with_signal" if matches
            else "proceed_to_llm_no_signal"
        ),
        "note": "Always run LLM judgment pass regardless of keyword result.",
    }


@mcp.tool()
def extract_timestamped_segments(
    url: str,
    language: str = "en",
    max_segments: int = 10,
) -> dict:
    """
    Extract timestamped subtitle segments from a video/podcast.
    Every podcast/video bullet must include a timestamp per project spec.
    """
    import json

    ydl_opts = make_ydl_opts(skip_download=True)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title")
        duration = info.get("duration")
        subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})

        if language not in subtitles:
            return {"title": title, "url": url, "segments": [], "error": f"No subtitles for language: {language}"}

        sub_url = None
        for fmt in subtitles[language]:
            if fmt.get("ext") == "json3":
                sub_url = fmt["url"]
                break
        if not sub_url:
            sub_url = subtitles[language][0]["url"]

        try:
            with urllib.request.urlopen(sub_url) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:
            return {"title": title, "url": url, "segments": [], "error": str(e)}

        segments = []
        try:
            data = json.loads(raw)
            for event in data.get("events", []):
                segs = event.get("segs")
                if not segs:
                    continue
                text = "".join(s.get("utf8", "") for s in segs).strip()
                if not text or text == "\n":
                    continue
                start_ms = event.get("tStartMs", 0)
                minutes = start_ms // 60000
                seconds = (start_ms % 60000) // 1000
                segments.append({"timestamp": f"~{minutes}:{seconds:02d}", "start_ms": start_ms, "text": text})
                if len(segments) >= max_segments:
                    break
        except Exception:
            segments = [{"timestamp": "N/A", "text": raw[:2000]}]

        return {
            "title": title,
            "url": url,
            "duration_seconds": duration,
            "language": language,
            "segments": segments,
            "segment_count": len(segments),
        }


@mcp.tool()
def score_relevance(
    text: str,
    monitoring_context: str,
    keyword_matches: list[str],
    source_title: str = "",
) -> dict:
    """
    Compute a rubric relevance score (0–100) for content against a monitoring context.
    Top-N items selected when too many qualify; threshold loosens on low-news days.
    """
    score = 0
    rationale = []

    kw_score = min(len(keyword_matches) * 10, 40)
    score += kw_score
    if keyword_matches:
        rationale.append(f"Keyword matches ({', '.join(keyword_matches[:5])}): +{kw_score}")

    context_words = set(monitoring_context.lower().split())
    text_words = set(text.lower().split())
    overlap = context_words & text_words
    context_score = min(int((len(overlap) / max(len(context_words), 1)) * 40), 40)
    score += context_score
    if overlap:
        rationale.append(f"Context overlap ({len(overlap)} shared terms): +{context_score}")

    length_score = min(len(text.split()) // 50, 20)
    score += length_score
    rationale.append(f"Content length signal: +{length_score}")

    default_threshold = 50
    passes = score >= default_threshold

    return {
        "source_title": source_title,
        "score": score,
        "max_score": 100,
        "passes_threshold": passes,
        "threshold_used": default_threshold,
        "rationale": rationale,
        "monitoring_context": monitoring_context,
        "recommendation": "include_in_digest" if passes else "exclude",
        "note": "Consider lowering threshold to 35 on low-news days per project spec." if not passes else "",
    }


@mcp.tool()
def format_digest_item(
    title: str,
    source_url: str,
    uploader: str,
    source_type: str,
    bullets: list[str],
    relevance_score: int,
    monitoring_context: str,
    upload_date: str = "",
    duration_seconds: int = 0,
) -> dict:
    """
    Format a single digest item into the standard Slack-ready structure.
    5–10 bullets, timestamps for audio/video, source attribution per project spec.
    """
    if len(bullets) > 10:
        bullets = bullets[:10]

    duration_str = f"{duration_seconds // 60} min" if duration_seconds else ""
    source_label = {
        "video": "Video", "podcast": "Podcast", "article": "Article",
        "newsletter": "Newsletter", "rss": "Article",
    }.get(source_type.lower(), source_type.capitalize())

    meta = f"[{uploader} · {source_label}" + (f" · {duration_str}" if duration_str else "") + "]"
    if upload_date and len(upload_date) == 8:
        meta += f" · {upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    bullet_lines = "\n".join(f"- {b}" for b in bullets)
    slack_block = (
        f"*{title}*\n{meta}\n_{monitoring_context}_\n\n"
        f"{bullet_lines}\n\n🔗 {source_url}\n📊 Relevance score: {relevance_score}/100"
    )

    return {
        "title": title,
        "source_url": source_url,
        "uploader": uploader,
        "source_type": source_type,
        "bullet_count": len(bullets),
        "bullets": bullets,
        "relevance_score": relevance_score,
        "monitoring_context": monitoring_context,
        "slack_formatted_block": slack_block,
        "passes_bullet_guardrail": 5 <= len(bullets) <= 10,
    }


@mcp.tool()
def build_digest(
    items: list[dict],
    issue_number: int,
    publish_date: str,
    max_items: int = 10,
) -> dict:
    """
    Assemble a full daily digest from scored and formatted items.
    Sorts by relevance score, caps at max_items, renders Slack message.
    """
    sorted_items = sorted(items, key=lambda x: x.get("relevance_score", 0), reverse=True)[:max_items]

    if not sorted_items:
        return {
            "issue_number": issue_number,
            "publish_date": publish_date,
            "item_count": 0,
            "slack_message": None,
            "post_decision": "skip — no qualifying items today; logged per spec.",
        }

    header = (
        f"🤖 *The Daily Crumb — Issue #{issue_number} · {publish_date}*\n"
        f"Your daily AI news digest for the Candidly engineering team.\n"
        f"{'─' * 40}"
    )
    body = "\n\n---\n\n".join(i.get("slack_formatted_block", "") for i in sorted_items)
    footer = f"{'─' * 40}\n📋 Full source list & attribution log in Notion.\n💬 Feedback? DM this bot directly."
    full_message = f"{header}\n\n{body}\n\n{footer}"

    return {
        "issue_number": issue_number,
        "publish_date": publish_date,
        "item_count": len(sorted_items),
        "items_included": [i.get("title") for i in sorted_items],
        "slack_message": full_message,
        "post_decision": "post",
        "char_count": len(full_message),
    }


@mcp.tool()
def check_duplicate(item_url: str, item_title: str, post_log: list[dict]) -> dict:
    """
    Check whether a digest item has already been posted using the post log.
    Prevents duplicate items across daily digest issues per project spec.
    """
    for entry in post_log:
        if entry.get("url") == item_url or entry.get("title") == item_title:
            return {
                "is_duplicate": True,
                "matched_on": "url" if entry.get("url") == item_url else "title",
                "previously_posted_in_issue": entry.get("issue_number"),
                "previously_posted_date": entry.get("publish_date"),
                "recommendation": "exclude",
            }
    return {"is_duplicate": False, "recommendation": "include", "url": item_url, "title": item_title}


@mcp.tool()
def start_podcast_transcription(url: str, language_code: str = "en-US") -> dict:
    """
    Start transcribing a podcast episode with ZERO local disk usage.
    Downloads audio into memory, uploads to S3, and kicks off an AWS
    Transcribe job — then returns immediately with a job_name.
    Call get_transcription_result(job_name) after ~2-5 minutes to fetch
    the completed transcript. This two-step approach works within Lambda
    time limits. Supports direct MP3/M4A URLs and tracking redirect links
    (swap.fm, podtrac, simplecast, etc.) as well as YouTube and other
    yt-dlp supported sources.
    """
    import io
    import uuid

    import boto3

    audio_s3_key = None
    region = os.environ.get("AWS_REGION", "us-east-1")

    # Step 1: Resolve the final audio stream URL and episode metadata.
    # First try yt-dlp (handles YouTube, Spotify, Apple Podcasts, etc.).
    # If yt-dlp doesn't recognise the URL (e.g. tracking redirect links
    # like swap.fm/podtrac/simplecast), fall back to following HTTP
    # redirects directly to the raw audio file.
    info: dict = {}
    stream_url: str | None = None
    audio_ext = "mp3"

    try:
        ydl_opts = make_ydl_opts(format="bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio/best")
        info = _extract_info_with_fallback(url, ydl_opts)

        formats = info.get("formats") or []

        def _format_rank(f):
            if not f.get("url") or f.get("acodec") == "none":
                return -1
            ext = f.get("ext", "")
            audio_only = f.get("vcodec") in (None, "none", "")
            ext_score = {"m4a": 4, "mp4": 3, "mp3": 2}.get(ext, 1)
            return ext_score * 2 + (1 if audio_only else 0)

        ranked = sorted(formats, key=_format_rank)
        best = ranked[-1] if ranked else {}
        stream_url = best.get("url")
        audio_ext = best.get("ext", "mp4")
    except Exception:
        pass  # fall through to direct-URL path

    # Fallback: treat the URL as a direct/redirect audio link (e.g. swap.fm, podtrac).
    # Only accept responses with an audio Content-Type — reject HTML pages so we
    # never pass a webpage URL to ffmpeg (which would fill disk with probe temp files).
    if not stream_url:
        try:
            req = urllib.request.Request(
                url,
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.url
                content_type = resp.headers.get("Content-Type", "")

            _audio_ct = {
                "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/ogg",
                "audio/flac", "audio/wav", "audio/webm", "audio/aac",
                "video/mp4", "video/webm",  # video containers often carry audio
            }
            ct_base = content_type.split(";")[0].strip().lower()
            if ct_base not in _audio_ct:
                return {
                    "error": (
                        f"URL resolved to a non-audio Content-Type ({ct_base!r}). "
                        "yt-dlp could not extract a stream URL for this source. "
                        "Provide a direct MP3/MP4/M4A file URL or a supported platform link."
                    ),
                    "url": url,
                    "step": "resolve_url",
                }

            _ct_map = {
                "audio/mpeg": "mp3", "audio/mp4": "mp4",
                "audio/x-m4a": "mp4", "audio/ogg": "ogg",
                "audio/flac": "flac", "audio/wav": "wav",
                "audio/webm": "webm", "audio/aac": "mp4",
                "video/mp4": "mp4", "video/webm": "webm",
            }
            audio_ext = _ct_map.get(ct_base, "mp3")
            stream_url = final_url
            info = {"title": None, "uploader": None, "duration": None,
                    "upload_date": None, "description": None}
        except Exception as exc:
            return {
                "error": f"Could not resolve audio URL (yt-dlp and direct fallback both failed): {exc}",
                "url": url,
                "step": "resolve_url",
                "hint": "Provide the episode page URL or a direct MP3/MP4 link.",
            }

    if not stream_url:
        return {"error": "Could not resolve a direct audio stream URL", "url": url, "step": "select_format"}

    import threading

    bucket = s3_utils.bucket_name()
    job_suffix = str(uuid.uuid4())[:8]
    safe_title = s3_utils._safe_key(info.get("title") or "podcast")

    # _resolve_ffmpeg_location returns the full binary path (imageio-ffmpeg uses
    # versioned names like "ffmpeg-linux-x86_64-v7.0.2", not just "ffmpeg").
    ffmpeg_bin = _resolve_ffmpeg_location() or shutil.which("ffmpeg")

    def _is_hls(url: str) -> bool:
        return "m3u8" in url or url.endswith(".m3u8")

    def _resolve_hls_segments(m3u8_url: str, _depth: int = 0) -> list[str]:
        """Parse an HLS manifest and return all media-segment URLs in order.
        Follows one level of master-playlist redirect automatically."""
        if _depth > 3:
            raise ValueError("Too many levels of HLS playlist nesting")
        req = urllib.request.Request(m3u8_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        base = m3u8_url.rsplit("/", 1)[0] + "/"
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # Master playlist? Follow the last (highest-quality) variant.
        if "#EXT-X-STREAM-INF" in text:
            variants = []
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                    nxt = lines[i + 1]
                    variants.append(nxt if nxt.startswith("http") else base + nxt)
            if variants:
                return _resolve_hls_segments(variants[-1], _depth + 1)
        # Media playlist: non-comment non-empty lines are segment URLs.
        segs = []
        for ln in lines:
            if not ln.startswith("#"):
                segs.append(ln if ln.startswith("http") else base + ln)
        return segs

    def _run_ffmpeg_with_stdin(
        ffmpeg_bin: str,
        input_iter,           # iterable of bytes chunks
        extra_args: list[str],
        s3_client,
        bucket: str,
        s3_key: str,
    ) -> tuple[int, str]:
        """Stream bytes from input_iter → ffmpeg stdin → ffmpeg stdout → S3.
        Returns (returncode, stderr_tail)."""
        cmd = [
            ffmpeg_bin, "-y",
            "-f", "mpegts", "-i", "pipe:0",
        ] + extra_args + ["pipe:1"]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            stderr_chunks.append(proc.stderr.read())

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        def _write_stdin() -> None:
            try:
                for chunk in input_iter:
                    proc.stdin.write(chunk)
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        write_thread = threading.Thread(target=_write_stdin, daemon=True)
        write_thread.start()

        try:
            s3_client.upload_fileobj(proc.stdout, bucket, s3_key)
        finally:
            proc.stdout.close()
            stderr_thread.join(timeout=30)
            write_thread.join(timeout=300)
            proc.wait()

        return proc.returncode, b"".join(stderr_chunks).decode(errors="replace")[-600:]

    _FFMPEG_AUDIO_ARGS = [
        "-t", "7200",               # cap at 2 hours
        "-vn",                      # drop video output
        "-acodec", "flac",          # built-in codec — never segfaults on any hardware
        "-ar", "16000",             # 16 kHz — AWS Transcribe standard for speech
        "-ac", "1",                 # mono
        "-compression_level", "0",  # fastest encode
        "-f", "flac",
    ]

    try:
        if ffmpeg_bin:
            audio_ext = "flac"
            audio_s3_key = f"podcast-audio/{safe_title}-{job_suffix}.{audio_ext}"
            s3_client = boto3.client("s3", region_name=region)

            if _is_hls(stream_url):
                # HLS: download segments in Python to bypass ffmpeg's HTTP/TLS client,
                # which crashes (SIGSEGV) on some container environments.
                # We pipe raw MPEG-TS bytes directly to ffmpeg stdin instead.
                try:
                    seg_urls = _resolve_hls_segments(stream_url)
                    print(f"[hls] resolved {len(seg_urls)} segments — streaming via stdin", flush=True)
                except Exception as exc:
                    return {"error": f"Failed to parse HLS manifest: {exc}", "url": url, "step": "hls_parse"}

                def _seg_iter():
                    req_hdrs = {"User-Agent": "Mozilla/5.0"}
                    for seg_url in seg_urls:
                        req = urllib.request.Request(seg_url, headers=req_hdrs)
                        with urllib.request.urlopen(req, timeout=60) as r:
                            yield r.read()

                rc, stderr_tail = _run_ffmpeg_with_stdin(
                    ffmpeg_bin, _seg_iter(), _FFMPEG_AUDIO_ARGS, s3_client, bucket, audio_s3_key,
                )
            else:
                # Non-HLS: pass URL directly to ffmpeg (safe, no HTTP-client crash risk).
                cmd = [ffmpeg_bin, "-y", "-i", stream_url] + _FFMPEG_AUDIO_ARGS + ["pipe:1"]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stderr_chunks_direct: list[bytes] = []
                def _drain_stderr_direct() -> None:
                    stderr_chunks_direct.append(proc.stderr.read())
                dt = threading.Thread(target=_drain_stderr_direct, daemon=True)
                dt.start()
                try:
                    s3_client.upload_fileobj(proc.stdout, bucket, audio_s3_key)
                finally:
                    proc.stdout.close()
                    dt.join()
                    proc.wait()
                rc = proc.returncode
                stderr_tail = b"".join(stderr_chunks_direct).decode(errors="replace")[-600:]

            if rc != 0:
                return {
                    "error": f"ffmpeg encode failed (exit {rc}): {stderr_tail}",
                    "url": url,
                    "step": "ffmpeg_encode",
                }
        else:
            # ffmpeg not available — fall back to raw download for direct audio URLs only.
            # HLS streams will not work via this path.
            is_hls = audio_ext in ("m3u8", "mpd") or "m3u8" in stream_url
            if is_hls:
                return {
                    "error": "ffmpeg is required to process HLS streams but was not found",
                    "url": url,
                    "step": "ffmpeg_missing",
                    "hint": "Install ffmpeg or set FFMPEG_LOCATION env var.",
                }
            req = urllib.request.Request(stream_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                audio_bytes = io.BytesIO(resp.read())
            audio_s3_key = f"podcast-audio/{safe_title}-{job_suffix}.{audio_ext}"
            s3_client = boto3.client("s3", region_name=region)
            audio_bytes.seek(0)
            s3_client.upload_fileobj(audio_bytes, bucket, audio_s3_key)

        s3_uri = f"s3://{bucket}/{audio_s3_key}"
    except Exception as exc:
        return {"error": f"Audio download/upload failed: {exc}", "url": url, "step": "download_or_upload"}

    try:
        # Step 4: Start AWS Transcribe job and return immediately — no polling.
        # Lambda will time out if we poll here; use get_transcription_result instead.
        transcribe = boto3.client("transcribe", region_name=region)
        job_name = f"podcast-{job_suffix}"
        transcript_s3_key = f"transcripts/{job_name}.json"

        _media_format_map = {
            "mp4": "mp4", "m4a": "mp4", "mp3": "mp3",
            "webm": "webm", "ogg": "ogg", "flac": "flac",
            "wav": "wav", "amr": "amr",
        }
        media_format = _media_format_map.get(audio_ext, "mp4")

        transcribe_kwargs: dict = {
            "TranscriptionJobName": job_name,
            "Media": {"MediaFileUri": s3_uri},
            "MediaFormat": media_format,
            "LanguageCode": language_code,
            "OutputBucketName": bucket,
            "OutputKey": transcript_s3_key,
        }
        # When ffmpeg normalised the audio we know the exact output sample rate,
        # so we pass it explicitly — this prevents Transcribe's auto-detection
        # from failing on unusual source rates.
        if ffmpeg_bin and audio_ext in ("mp3", "flac", "wav"):
            transcribe_kwargs["MediaSampleRateHertz"] = 16000
        transcribe.start_transcription_job(**transcribe_kwargs)
    except Exception as exc:
        return {
            "error": f"Failed to start AWS Transcribe job: {exc}",
            "url": url,
            "audio_ext": audio_ext,
            "s3_key": audio_s3_key,
            "step": "start_transcribe",
        }

    return {
        "status": "transcription_started",
        "job_name": job_name,
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "audio_format": media_format,
        "transcript_s3_key": transcript_s3_key,
        "url": url,
        "next_step": "Call get_transcription_result with the job_name in 2-5 minutes.",
    }


@mcp.tool()
def get_transcription_result(job_name: str) -> dict:
    """
    Fetch the result of a transcription job started by start_podcast_transcription.
    Returns the transcript text if the job is complete, or status if still in progress.
    Call this 2-5 minutes after start_podcast_transcription returns a job_name.
    If status is IN_PROGRESS, wait a bit longer and call again.
    """
    import json

    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        transcribe = boto3.client("transcribe", region_name=region)
        response = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        job = response["TranscriptionJob"]
        status = job["TranscriptionJobStatus"]
    except Exception as exc:
        return {"error": f"Could not retrieve job status: {exc}", "job_name": job_name}

    if status == "IN_PROGRESS" or status == "QUEUED":
        return {
            "status": status,
            "job_name": job_name,
            "message": "Transcription is still running. Call again in 1-2 minutes.",
        }

    if status == "FAILED":
        return {
            "status": "FAILED",
            "job_name": job_name,
            "error": job.get("FailureReason", "unknown"),
        }

    # COMPLETED — fetch the transcript JSON from S3
    try:
        bucket = s3_utils.bucket_name()
        transcript_s3_key = f"transcripts/{job_name}.json"
        s3_client = boto3.client("s3", region_name=region)
        result_obj = s3_client.get_object(Bucket=bucket, Key=transcript_s3_key)
        transcript_data = json.loads(result_obj["Body"].read())
        transcript_text = transcript_data["results"]["transcripts"][0]["transcript"]
    except Exception as exc:
        return {"error": f"Job completed but failed to fetch transcript: {exc}", "job_name": job_name}

    return {
        "status": "COMPLETED",
        "job_name": job_name,
        "transcript": transcript_text,
        "transcript_s3_key": transcript_s3_key,
    }


@mcp.tool()
def normalize_youtube_url(url: str) -> dict:
    """
    Convert any YouTube URL variant to the canonical https://www.youtube.com/watch?v=ID form.
    Handles: youtu.be short links, YouTube Shorts (/shorts/), YouTube Live (/live/),
    mobile links (m.youtube.com), embedded player URLs (/embed/), and URLs with
    tracking or playlist parameters that obscure the video ID.
    Use this before passing a YouTube URL to any other tool to ensure compatibility.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path
    video_id: str | None = None

    if host == "youtu.be":
        video_id = path.lstrip("/").split("/")[0].split("?")[0]
    elif host == "youtube.com":
        if path.startswith("/watch"):
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        elif path.startswith("/shorts/"):
            video_id = path.split("/shorts/")[1].split("/")[0].split("?")[0]
        elif path.startswith("/live/"):
            video_id = path.split("/live/")[1].split("/")[0].split("?")[0]
        elif path.startswith("/embed/"):
            video_id = path.split("/embed/")[1].split("/")[0].split("?")[0]
        elif path.startswith("/v/"):
            video_id = path.split("/v/")[1].split("/")[0].split("?")[0]

    if not video_id or len(video_id) < 5:
        return {
            "error": "Could not extract a YouTube video ID from this URL",
            "url": url,
            "hint": "Provide a YouTube video URL (watch, youtu.be, Shorts, or Live link).",
        }

    _format_labels = {
        "youtu.be": "short link (youtu.be)",
        "/shorts/": "YouTube Shorts",
        "/live/": "YouTube Live",
        "/embed/": "embed URL",
        "/v/": "legacy /v/ URL",
    }
    original_format = "standard watch URL"
    if host == "youtu.be":
        original_format = "short link (youtu.be)"
    else:
        for fragment, label in _format_labels.items():
            if fragment in path:
                original_format = label
                break

    return {
        "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
        "video_id": video_id,
        "original_url": url,
        "original_format": original_format,
    }


@mcp.tool()
def get_transcript_text(url: str, language: str = "en") -> dict:
    """
    Get the full transcript text for any video or audio URL in a single call.

    YouTube strategy (free methods first, paid last):
      1. S3 cache (instant, free)
      2. yt-dlp iOS client + YouTube timedtext API (free)
      3. Invidious public API (free)
      4. youtube-transcript-api with cookie session (free)
      5. Supadata API (last resort — set SUPADATA_API_KEY; 100 free req/month)

    Non-YouTube strategy (Dailymotion, podcasts, direct URLs, etc.):
      1. S3 cache (instant, free)
      2. yt-dlp native subtitles (free, instant when available)
      3. AWS Transcribe — streams audio via ffmpeg to S3, transcribes
         (may take 2-5 minutes; no Supadata used for non-YouTube sources)

    Returns: transcript_text, url, language, transcript_source, char_count,
             and for YouTube: title, uploader, duration_seconds, upload_date.
    """
    import json
    import time

    language_code_map = {
        "en": "en-US", "fr": "fr-FR", "de": "de-DE", "es": "es-ES",
        "it": "it-IT", "pt": "pt-BR", "nl": "nl-NL", "ja": "ja-JP",
        "ko": "ko-KR", "zh": "zh-CN",
    }

    # --- S3 cache helpers (silently skip if S3_BUCKET not configured) ---
    def _cache_key_for(identifier: str) -> str:
        """Normalise identifier into a safe S3 key segment."""
        import re as _re
        return _re.sub(r"[^\w\-]", "_", identifier)[:200]

    def _load_cache(identifier: str) -> str | None:
        try:
            return s3_utils.get_transcript(_cache_key_for(identifier))
        except Exception:
            return None

    def _save_cache(identifier: str, text: str, source: str) -> None:
        try:
            s3_utils.put_transcript(_cache_key_for(identifier), text)
            print(f"[transcript-cache] saved {source} → transcripts/{_cache_key_for(identifier)}.txt", flush=True)
        except Exception:
            pass

    # --- Fast path: YouTube-direct transcript (no yt-dlp, no bot check) ---
    yt_id = _youtube_video_id(url)
    if yt_id:
        # Check S3 cache first — avoids burning Supadata credits on re-fetches
        cache_id = f"youtube_{yt_id}_{language}"
        cached = _load_cache(cache_id)
        if cached:
            meta = _youtube_oembed_meta(yt_id)
            print(f"[transcript-cache] hit youtube/{yt_id}/{language}", flush=True)
            return {
                "title": meta.get("title"),
                "uploader": meta.get("uploader"),
                "duration_seconds": meta.get("duration_seconds"),
                "upload_date": meta.get("upload_date"),
                "url": url,
                "language": language,
                "transcript_text": cached,
                "transcript_source": "s3_cache",
                "char_count": len(cached),
            }

        text, transcript_error = _fetch_youtube_transcript(yt_id, language)
        if text:
            _save_cache(cache_id, text, f"youtube/{yt_id}/{language}")
            meta = _youtube_oembed_meta(yt_id)
            return {
                "title": meta.get("title"),
                "uploader": meta.get("uploader"),
                "duration_seconds": meta.get("duration_seconds"),
                "upload_date": meta.get("upload_date"),
                "url": url,
                "language": language,
                "transcript_text": text,
                "transcript_source": "youtube_transcript_api",
                "char_count": len(text),
            }

        # If youtube-transcript-api isn't installed yet, surface that clearly
        # rather than falling through to the audio path (which will also fail for YouTube).
        if transcript_error and "not installed" in transcript_error:
            return {
                "url": url,
                "transcript_text": None,
                "transcript_source": "failed",
                "error": (
                    "youtube-transcript-api is not installed in this environment. "
                    "The server needs to be restarted or redeployed. "
                    f"Detail: {transcript_error}"
                ),
            }

        # All free methods + Supadata failed — surface the error clearly.
        meta = _youtube_oembed_meta(yt_id)
        return {
            **meta,
            "url": url,
            "transcript_text": None,
            "transcript_source": "failed",
            "error": transcript_error,
        }

    # ------------------------------------------------------------------
    # Non-YouTube: S3 cache → yt-dlp native subtitles → AWS Transcribe
    # Supadata is YouTube-specific and is never used here.
    # ------------------------------------------------------------------
    import hashlib as _hashlib
    import re as _re
    url_hash = _hashlib.sha1(url.encode()).hexdigest()[:16]
    cache_id = f"url_{url_hash}_{language}"
    cached = _load_cache(cache_id)
    if cached:
        print(f"[transcript-cache] hit url/{url_hash}/{language}", flush=True)
        return {
            "url": url,
            "language": language,
            "transcript_text": cached,
            "transcript_source": "s3_cache",
            "char_count": len(cached),
        }

    # --- Step 1: try native subtitles via yt-dlp (free, instant) ---
    # Do NOT pass writesubtitles/writeautomaticsub — those cause yt-dlp to write
    # .vtt files to disk even with skip_download=True.  We extract the caption URLs
    # from the info dict and download them in memory ourselves (see below).
    title = uploader = duration = upload_date = None
    try:
        ydl_opts = make_ydl_opts(skip_download=True)
        info = _extract_info_with_fallback(url, ydl_opts)
        title = info.get("title")
        uploader = info.get("uploader")
        duration = info.get("duration")
        upload_date = info.get("upload_date")

        subtitles = info.get("subtitles", {}) or {}
        auto_caps = info.get("automatic_captions", {}) or {}
        all_subs = subtitles if language in subtitles else auto_caps
        caption_url = caption_ext = None
        for fmt in all_subs.get(language, []):
            if fmt.get("ext") in ("json3", "vtt", "srv3"):
                caption_url = fmt.get("url")
                caption_ext = fmt.get("ext")
                break

        if caption_url:
            with urllib.request.urlopen(caption_url, timeout=30) as resp:
                raw = resp.read().decode("utf-8")

            if caption_ext == "json3":
                data = json.loads(raw)
                parts = [
                    "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()
                    for ev in data.get("events", [])
                ]
                transcript_text = " ".join(p for p in parts if p and p != "\n")
            else:
                lines = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or "-->" in line or line.isdigit():
                        continue
                    line = _re.sub(r"<[^>]+>", "", line)
                    if line:
                        lines.append(line)
                transcript_text = " ".join(lines)

            if transcript_text:
                _save_cache(cache_id, transcript_text, f"native_captions {url[:60]}")
                return {
                    "title": title, "uploader": uploader,
                    "duration_seconds": duration, "upload_date": upload_date,
                    "url": url, "language": language,
                    "transcript_text": transcript_text,
                    "transcript_source": "native_captions",
                    "char_count": len(transcript_text),
                }
    except Exception:
        pass  # no subtitles or extraction failed — fall through to AWS Transcribe

    # --- Step 2: no native subtitles → AWS Transcribe ---
    language_code = language_code_map.get(language, f"{language}-{language.upper()}")
    job = start_podcast_transcription(url, language_code=language_code)

    if job.get("error"):
        return {
            "title": title, "uploader": uploader, "url": url,
            "transcript_text": None,
            "error": job["error"],
            "transcript_source": "failed",
        }

    job_name = job.get("job_name")

    for _ in range(48):
        time.sleep(10)
        result = get_transcription_result(job_name)
        status = result.get("status")
        if status == "COMPLETED":
            transcript = result.get("transcript") or ""
            if transcript:
                _save_cache(cache_id, transcript, f"aws_transcribe {url[:60]}")
            return {
                "title": title, "uploader": uploader,
                "duration_seconds": duration, "upload_date": upload_date,
                "url": url, "language": language,
                "transcript_text": transcript,
                "transcript_source": "aws_transcribe",
                "char_count": len(transcript),
            }
        if status == "FAILED":
            return {
                "title": title, "uploader": uploader, "url": url,
                "transcript_text": None,
                "error": result.get("error", "AWS Transcribe job failed"),
                "transcript_source": "failed",
            }

    return {
        "title": title, "uploader": uploader, "url": url,
        "transcript_text": None,
        "transcript_source": "timeout",
        "job_name": job_name,
        "next_step": f"Call get_transcription_result('{job_name}') to check.",
    }


@mcp.tool()
def check_server_config() -> dict:
    """
    Diagnostic tool — shows the server's current runtime configuration.
    Use this to verify cookies, proxy, ffmpeg, and AWS settings are loaded correctly
    before troubleshooting transcript or audio extraction failures.
    """
    cookies_raw = os.environ.get("YTDLP_COOKIES_FILE", "")
    cookies_resolved = os.environ.get("YTDLP_COOKIES_FILE", "")
    cookies_ok = bool(cookies_resolved and Path(cookies_resolved).is_file())
    cookies_size = Path(cookies_resolved).stat().st_size if cookies_ok else None

    ffmpeg_bin = _resolve_ffmpeg_location() or shutil.which("ffmpeg")

    try:
        yt_api_version = __import__("youtube_transcript_api").__version__
    except Exception:
        yt_api_version = "not installed"

    try:
        curl_version = __import__("curl_cffi").__version__
    except Exception:
        curl_version = "not installed"

    supadata_key = os.environ.get("SUPADATA_API_KEY", "")
    return {
        "youtube_transcript": {
            "supadata_api_key": "set ✓" if supadata_key else "NOT SET — recommended to bypass IP blocks",
            "proxy": os.environ.get("YTDLP_PROXY") or os.environ.get("HTTPS_PROXY") or "(not set)",
            "note": (
                "SUPADATA_API_KEY is the easiest fix for cloud IP blocks. "
                "Get a free key (100 req/mo) at dash.supadata.ai"
                if not supadata_key else
                "Supadata API will be used as primary transcript source."
            ),
        },
        "cookies": {
            "env_value": cookies_raw or "(not set)",
            "resolved_path": cookies_resolved or "(not set)",
            "file_exists": cookies_ok,
            "file_size_bytes": cookies_size,
            "status": "OK — cookies will be used" if cookies_ok else
                      "MISSING — YouTube requests may be IP-blocked without SUPADATA_API_KEY",
        },
        "proxy": {
            "YTDLP_PROXY": os.environ.get("YTDLP_PROXY", "(not set)"),
            "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "(not set)"),
        },
        "ffmpeg": {
            "binary": ffmpeg_bin or "(not found)",
            "available": bool(ffmpeg_bin),
        },
        "dependencies": {
            "youtube_transcript_api": yt_api_version,
            "curl_cffi": curl_version,
        },
        "aws": {
            "S3_BUCKET": os.environ.get("S3_BUCKET", "(not set)"),
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        },
    }


if __name__ == "__main__":
    mcp.run()
