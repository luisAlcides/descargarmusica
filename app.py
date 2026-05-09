from __future__ import annotations

import ipaddress
import os
import re
import socket
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

    if not fmt or fmt not in ALLOWED_EXTENSIONS:
        fmt = "mp3"

    try:
        validate_url(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "starting",
        "progress": "0%",
        "error": None,
        "filename": filename,
        "fmt": fmt
    }

    thread = threading.Thread(target=process_download, args=(task_id, url, fmt, filename))
    thread.start()

    return jsonify({"task_id": task_id})


@app.get("/api/progress/<task_id>")
def api_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


def process_download(task_id, url, fmt, filename):
    out_tmpl = str(DOWNLOAD_DIR / f"{task_id}.%(ext)s")

    def progress_hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            percent = re.sub(r'\x1b\[[0-9;]*m', '', percent)
            tasks[task_id]['progress'] = percent
            tasks[task_id]['status'] = 'downloading'
        elif d['status'] == 'finished':
            tasks[task_id]['status'] = 'processing'
            tasks[task_id]['progress'] = '100%'

    ydl_opts = {
        'format': 'bestaudio/best' if fmt in ['mp3', 'm4a', 'wav', 'aac', 'flac', 'ogg', 'opus'] else 'bestvideo+bestaudio/best',
        'outtmpl': out_tmpl,
        'noplaylist': True,
        'quiet': True,
        'no_color': True,
        'progress_hooks': [progress_hook],
        'extractor_args': {
            'youtube': ['player_client=tv,ios']
        }
    }

    # Use cookies if available
    cookies_file = BASE_DIR / "cookies.txt"
    if cookies_file.exists():
        ydl_opts['cookiefile'] = str(cookies_file)
    elif os.environ.get("YOUTUBE_COOKIES"):
        import tempfile
        fd, temp_cookies_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, 'w') as f:
            f.write(os.environ.get("YOUTUBE_COOKIES"))
        ydl_opts['cookiefile'] = temp_cookies_path

    if fmt in ['mp3', 'm4a', 'wav', 'aac', 'flac', 'ogg', 'opus']:
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
                
    except Exception as exc:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(exc)


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
