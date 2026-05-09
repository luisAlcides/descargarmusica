from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, abort, request, send_file, jsonify
from werkzeug.utils import secure_filename


app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def _resolve_ffmpeg_location() -> str | None:
    """Return a directory containing ffmpeg/ffprobe, downloading them if needed.

    Tries the system PATH first; falls back to the static-ffmpeg package which
    ships portable binaries that work on Linux, macOS and Windows.
    """
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return str(Path(ffmpeg_path).parent)

    try:
        from static_ffmpeg import add_paths  # type: ignore

        add_paths()
    except Exception:
        return None

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return str(Path(ffmpeg_path).parent)
    return None


FFMPEG_LOCATION = _resolve_ffmpeg_location()


def _resolve_pot_script() -> str | None:
    """Locate the bgutil-ytdlp-pot-provider generate_once.js script.

    Checks the env var ``BGUTIL_POT_SCRIPT`` first, then a few default
    locations that match the build phase of nixpacks.toml.
    """
    candidates = [
        os.environ.get("BGUTIL_POT_SCRIPT"),
        "/app/bgutil-pot/server/build/generate_once.js",
        str(BASE_DIR / "bgutil-pot" / "server" / "build" / "generate_once.js"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


POT_SCRIPT_PATH = _resolve_pot_script()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

ALLOWED_EXTENSIONS = {
    "aac",
    "flac",
    "m4a",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "ogg",
    "opus",
    "wav",
    "webm",
}

# Store task progress
tasks = {}

@app.get("/")
def home():
    return send_file(BASE_DIR / "index.html")


@app.post("/api/start")
def api_start():
    data = request.json or {}
    url = data.get("url", "").strip()
    filename = secure_filename(data.get("filename", "").strip()) or "archivo-multimedia"
    fmt = data.get("format", "").strip().lower()
    cookies_text = (data.get("cookies") or "").strip()

    if not fmt or fmt not in ALLOWED_EXTENSIONS:
        fmt = "mp3"

    try:
        validate_url(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if cookies_text and not _looks_like_netscape_cookies(cookies_text):
        return jsonify({
            "error": "El formato de las cookies no es valido. "
                     "Exportalas con una extension tipo \"Get cookies.txt LOCALLY\" "
                     "(formato Netscape)."
        }), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "starting",
        "progress": "0%",
        "error": None,
        "filename": filename,
        "fmt": fmt
    }

    thread = threading.Thread(
        target=process_download,
        args=(task_id, url, fmt, filename, cookies_text),
    )
    thread.start()

    return jsonify({"task_id": task_id})


def _looks_like_netscape_cookies(text: str) -> bool:
    """Quick sanity check that the pasted text resembles a cookies.txt file."""
    if "# Netscape HTTP Cookie File" in text:
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


@app.get("/api/progress/<task_id>")
def api_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


def process_download(task_id, url, fmt, filename, cookies_text=""):
    out_tmpl = str(DOWNLOAD_DIR / f"{task_id}.%(ext)s")
    temp_cookies_path = None

    def progress_hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            percent = re.sub(r'\x1b\[[0-9;]*m', '', percent)
            tasks[task_id]['progress'] = percent
            tasks[task_id]['status'] = 'downloading'
        elif d['status'] == 'finished':
            tasks[task_id]['status'] = 'processing'
            tasks[task_id]['progress'] = '100%'

    is_audio = fmt in ['mp3', 'm4a', 'wav', 'aac', 'flac', 'ogg', 'opus']

    if is_audio:
        format_selector = 'bestaudio*/bestaudio/best/bv*+ba/b/worst'
    else:
        format_selector = 'bv*+ba/b/bestvideo+bestaudio/best/worst'

    if cookies_text:
        client_strategies = [
            'web_safari,mweb,web',
            'mweb,web_safari',
            'web,android',
        ]
    else:
        client_strategies = [
            'default,mweb,tv_simply',
            'web_safari,mweb,tv_simply',
            'tv_simply,mweb',
        ]

    cookie_source = _select_cookie_source(cookies_text)
    cookiefile = None
    if cookie_source:
        cookiefile, temp_cookies_path = cookie_source

    last_error = None
    last_log = ""

    try:
        for strategy_index, clients in enumerate(client_strategies):
            log_buffer = _YdlLogger()
            youtube_args = [f'player_client={clients}']
            if POT_SCRIPT_PATH:
                youtube_args.append(f'getpot_bgutil_script={POT_SCRIPT_PATH}')

            ydl_opts = {
                'format': format_selector,
                'outtmpl': out_tmpl,
                'noplaylist': True,
                'quiet': True,
                'no_color': True,
                'progress_hooks': [progress_hook],
                'retries': 5,
                'fragment_retries': 5,
                'extractor_retries': 3,
                'logger': log_buffer,
                'http_headers': {
                    'User-Agent': DEFAULT_USER_AGENT,
                    'Accept-Language': 'en-US,en;q=0.9',
                },
                'extractor_args': {
                    'youtube': youtube_args,
                },
            }

            if FFMPEG_LOCATION:
                ydl_opts['ffmpeg_location'] = FFMPEG_LOCATION

            if cookiefile:
                ydl_opts['cookiefile'] = cookiefile

            if is_audio:
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': fmt,
                    'preferredquality': '192',
                }]
            elif fmt in ['mp4', 'webm', 'mkv', 'mov']:
                ydl_opts['merge_output_format'] = fmt

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)

                extracted_title = info.get('title')
                if extracted_title:
                    safe_title = secure_filename(extracted_title)
                    if safe_title:
                        filename = f"{safe_title}.{fmt}"
                else:
                    if "." in filename:
                        base_name = filename.rsplit(".", 1)[0]
                        filename = f"{base_name}.{fmt}"
                    else:
                        filename = f"{filename}.{fmt}"

                final_filepath = DOWNLOAD_DIR / f"{task_id}.{fmt}"

                if not final_filepath.exists():
                    downloaded_files = list(DOWNLOAD_DIR.glob(f"{task_id}.*"))
                    if not downloaded_files:
                        raise Exception("Error al procesar el archivo.")
                    final_filepath = downloaded_files[0]

                tasks[task_id]['final_filename'] = filename
                tasks[task_id]['final_filepath'] = str(final_filepath)
                tasks[task_id]['status'] = 'done'
                return

            except Exception as exc:
                last_error = exc
                last_log = log_buffer.text()
                for leftover in DOWNLOAD_DIR.glob(f"{task_id}.*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
                continue

        message = str(last_error) if last_error else "Error desconocido."
        if last_log:
            tail = "\n".join(last_log.strip().splitlines()[-6:])
            message = f"{message}\n\nDetalles:\n{tail}"
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = message
    finally:
        if temp_cookies_path:
            try:
                os.remove(temp_cookies_path)
            except OSError:
                pass


class _YdlLogger:
    """Captures yt-dlp log output so we can surface it on errors."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        self._lines.append(msg)

    def info(self, msg: str) -> None:
        self._lines.append(msg)

    def warning(self, msg: str) -> None:
        self._lines.append(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self._lines.append(f"ERROR: {msg}")

    def text(self) -> str:
        return "\n".join(self._lines)


def _select_cookie_source(user_cookies_text: str) -> tuple[str, str | None] | None:
    """Pick the cookie source for yt-dlp.

    Returns a tuple ``(cookiefile_path, temp_path_to_cleanup_or_None)`` or
    ``None`` if no cookies are available. User-provided cookies take priority,
    then a ``cookies.txt`` next to the app, then the ``YOUTUBE_COOKIES``
    environment variable.
    """
    if user_cookies_text:
        path = _write_temp_cookies(user_cookies_text)
        return path, path

    cookies_file = BASE_DIR / "cookies.txt"
    if cookies_file.exists():
        return str(cookies_file), None

    env_cookies = os.environ.get("YOUTUBE_COOKIES")
    if env_cookies:
        path = _write_temp_cookies(env_cookies)
        return path, path

    return None


def _write_temp_cookies(content: str) -> str:
    """Write the given cookies content to a temp file and return its path."""
    fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if "# Netscape HTTP Cookie File" not in content:
            f.write("# Netscape HTTP Cookie File\n")
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")
    return temp_path


@app.get("/download/<task_id>")
def download_file(task_id):
    task = tasks.get(task_id)
    if not task or task['status'] != 'done':
        abort(404, "Archivo no encontrado o no esta listo.")

    final_filepath = Path(task['final_filepath'])
    filename = task['final_filename']
    fmt = task['fmt']

    if not final_filepath.exists():
        abort(404, "El archivo ya no existe.")

    def generate():
        try:
            with open(final_filepath, 'rb') as f:
                while chunk := f.read(1024 * 128):
                    yield chunk
        finally:
            try:
                os.remove(final_filepath)
                # Cleanup task
                if task_id in tasks:
                    del tasks[task_id]
            except Exception:
                pass

    content_type = "application/octet-stream"
    if fmt == "mp3": content_type = "audio/mpeg"
    elif fmt == "mp4": content_type = "video/mp4"
    elif fmt == "webm": content_type = "video/webm"
    elif fmt == "wav": content_type = "audio/wav"
    elif fmt == "m4a": content_type = "audio/mp4"

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": content_type,
        "Content-Length": str(final_filepath.stat().st_size)
    }

    return Response(generate(), headers=headers)


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        abort(400, "Usa una URL valida http:// o https://.")
    
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if is_private_host(host):
        abort(400, "No se permiten URLs locales o privadas.")


def is_private_host(host: str) -> bool:
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        abort(400, "No se pudo resolver el dominio.")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True

    return False


if __name__ == "__main__":
    app.run(debug=True)
