"""FastAPI-Backend: Uploads, EKG-Analyse und Video-Streaming."""

from __future__ import annotations

import mimetypes
import os
import re
import secrets
import shutil
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import analysis, scp_reader
from .scp_reader import DEFAULT_FS, RecordInfo, discover_records, load_measurement

DATA_ROOT = os.environ.get("DATA_ROOT", "/data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
UPLOAD_DIR = os.path.join(DATA_ROOT, "uploads")
UPLOAD_MAX_AGE_S = 24 * 3600

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
ECG_FILE_OK = (".scp",)

app = FastAPI(title="gdmt-ekg")

_lock = threading.Lock()
_record_cache: dict[tuple[str, str], tuple[RecordInfo, object]] = {}
_analysis_cache: dict[str, dict] = {}


def _prune_uploads() -> None:
    """Hochgeladene Dateien älter als UPLOAD_MAX_AGE_S entfernen."""
    try:
        if not os.path.isdir(UPLOAD_DIR):
            return
        for name in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, name)
            try:
                if (
                    os.path.isdir(p)
                    and os.path.getmtime(p) < time.time() - UPLOAD_MAX_AGE_S
                ):
                    shutil.rmtree(p)
            except OSError:
                pass
    except OSError:
        pass


_prune_uploads()


def _abs(path: str) -> str:
    """Relativen Pfad gegen DATA_ROOT auflösen; Traversal verhindern."""
    path = (path or "").strip().strip("/")
    full = os.path.normpath(os.path.join(DATA_ROOT, path))
    root = os.path.normpath(DATA_ROOT)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Pfad außerhalb des Datenordners")
    return full


def _rel(path: str) -> str:
    full = os.path.normpath(path)
    root = os.path.normpath(DATA_ROOT)
    rel = os.path.relpath(full, root)
    return "" if rel == "." else rel


def _get_record(folder_rel: str, record_no: int) -> RecordInfo:
    key = (_rel(folder_rel), str(record_no))
    with _lock:
        if key in _record_cache:
            return _record_cache[key][0]
    records = discover_records(_abs(folder_rel))
    for rec in records:
        if rec.no == record_no:
            with _lock:
                _record_cache[key] = (rec, None)
            return rec
    raise HTTPException(status_code=404, detail="Messung nicht gefunden")


def _measurement_for(folder_rel: str, record_no: int, fs: float):
    key = (_rel(folder_rel), str(record_no))
    with _lock:
        cached = _record_cache.get(key)
        if cached and cached[1] is not None and cached[1].fs == fs:
            return cached[1]
    rec = _get_record(folder_rel, record_no)
    measurement = load_measurement(rec, fs=fs)
    with _lock:
        if len(_record_cache) > 16:
            _record_cache.clear()
        _record_cache[key] = (rec, measurement)
    return measurement


def _save_upload(upload: UploadFile, dest_dir: str) -> str:
    """Upload-Datei unter dest_dir ablegen, Rückgabe des Zielpfads."""
    name = os.path.basename(upload.filename or "").strip()
    if not name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    with open(dest, "wb") as out:
        while chunk := upload.file.read(1 << 16):
            out.write(chunk)
    return dest


@app.post("/api/upload/video")
def upload_video(file: UploadFile = File(...)) -> dict:
    """Video manuell hochladen; liefert den Pfad für /api/video zurück."""
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Nicht unterstütztes Videoformat '{ext or '—'}' (erlaubt: {', '.join(sorted(VIDEO_EXTS))})",
        )
    session = secrets.token_hex(6)
    dest = _save_upload(file, os.path.join(UPLOAD_DIR, f"v-{session}"))
    return {"path": _rel(dest), "name": os.path.basename(dest)}


@app.post("/api/upload/ecg")
def upload_ecg(
    files: list[UploadFile] = File(...), session: str | None = Form(None)
) -> dict:
    """SCP-Dateien und/oder README.TXT in eine Upload-Session hochladen.

    Ohne 'session' wird eine neue Session angelegt; mit 'session' werden
    die Dateien in die bestehende Session eingefügt (z. B. erst SCP-Dateien,
    später die README.TXT). Die Messungserkennung erfolgt später über
    /api/measurements, sobald SCP-Dateien vorliegen.
    """
    if session is None:
        session = f"e-{secrets.token_hex(6)}"
    elif not re.fullmatch(r"e-[0-9a-f]{6,32}", session):
        raise HTTPException(status_code=400, detail="Ungültige Session-ID")
    dest_dir = os.path.join(UPLOAD_DIR, session)
    saved: list[str] = []
    for f in files:
        name = os.path.basename(f.filename or "")
        lower = name.lower()
        if lower.endswith(ECG_FILE_OK) or lower == "readme.txt":
            saved.append(os.path.basename(_save_upload(f, dest_dir)))
        else:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' wird ignoriert: nur .scp-Dateien und README.TXT",
            )
    scp_count = sum(1 for s in saved if s.lower().endswith(".scp"))
    return {
        "path": _rel(dest_dir),
        "session": session,
        "files": sorted(saved),
        "scp_count": scp_count,
    }


@app.post("/api/upload/clear")
def upload_clear(
    video: str | None = Form(None), session: str | None = Form(None)
) -> dict:
    """Hochgeladene Dateien (Upload-Cache) löschen und Auswahl zurücksetzen.

    'session' entfernt die EKG-Upload-Session (uploads/e-…), 'video' die
    hochgeladene Videodatei (uploads/v-…). Serverseitige Analyse-Caches der
    betroffenen Ordner werden ebenfalls verworfen.
    """
    deleted: list[str] = []
    if session is not None:
        if not re.fullmatch(r"e-[0-9a-f]{6,32}", session):
            raise HTTPException(status_code=400, detail="Ungültige Session-ID")
        dest = os.path.join(UPLOAD_DIR, session)
        if os.path.isdir(dest):
            rel_dest = _rel(dest)
            shutil.rmtree(dest)
            deleted.append(rel_dest)
            with _lock:
                for key in [k for k in _analysis_cache if k.split("|", 1)[0] == rel_dest]:
                    del _analysis_cache[key]
                for key in [k for k in _record_cache if k[0] == rel_dest]:
                    del _record_cache[key]
    if video is not None:
        full = _abs(video)
        up_root = os.path.normpath(UPLOAD_DIR)
        inside = full.startswith(up_root + os.sep) and os.path.isfile(full)
        if not inside:
            raise HTTPException(
                status_code=400, detail="Nur hochgeladene Dateien können gelöscht werden"
            )
        folder = os.path.normpath(os.path.dirname(full))
        if folder == up_root:
            os.unlink(full)
        else:
            shutil.rmtree(folder)
        deleted.append(_rel(full))
    return {"deleted": deleted}


@app.get("/api/measurements")
def measurements(folder: str = "") -> dict:
    """Messungen (Records) eines Ordners laut README.TXT bzw. Fallback."""
    records = discover_records(_abs(folder))
    return {
        "records": [
            {
                "no": r.no,
                "label": r.label,
                "files": [os.path.basename(f) for f in r.files],
                "source": r.source,
            }
            for r in records
        ]
    }


@app.get("/api/ecg")
def ecg(
    folder: str = Query(...),
    record: int = Query(...),
    fs: float = Query(DEFAULT_FS, gt=0, le=1000),
) -> dict:
    """Vollständige Analyse einer Messung (Pan-Tompkins + Signaldaten)."""
    cache_key = f"{_rel(folder)}|{record}|{fs}"
    with _lock:
        if cache_key in _analysis_cache:
            return _analysis_cache[cache_key]

    measurement = _measurement_for(folder, record, fs)
    if len(measurement.signal) == 0:
        raise HTTPException(status_code=404, detail="Keine SCP-Daten gefunden")

    beats = analysis.pan_tompkins(measurement.signal, measurement.fs)
    t_mid, rr, hr = analysis.heart_rate_from_beats(beats, measurement.fs)

    payload = {
        "fs": measurement.fs,
        "n_samples": int(len(measurement.signal)),
        "duration": measurement.duration,
        "files": [os.path.basename(f) for f in measurement.files],
        "signal": measurement.signal.astype(int).tolist(),
        "beats_idx": [int(b) for b in beats],
        "beats_t": (beats / measurement.fs).round(4).tolist(),
        "device_beats_idx": [int(b) for b in measurement.device_beats],
        "hr_t": t_mid.round(4).tolist(),
        "hr": hr.round(2).tolist(),
        "rr": rr.round(4).tolist(),
    }
    with _lock:
        if len(_analysis_cache) > 8:
            _analysis_cache.clear()
        _analysis_cache[cache_key] = payload
    return payload


@app.get("/api/video")
def video(path: str, request: Request):
    """Video mit HTTP-Range-Unterstützung streamen."""
    full = _abs(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    media_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
    size = os.path.getsize(full)

    range_hdr = request.headers.get("range")
    if range_hdr:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_hdr.strip())
        if not m:
            raise HTTPException(status_code=416, detail="Ungültiger Range-Header")
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            raise HTTPException(status_code=416, detail="Range außerhalb der Datei")
        length = end - start + 1

        def generate():
            with open(full, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            generate(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )
    return FileResponse(full, media_type=media_type)


@app.middleware("http")
async def _caching_header(request: Request, call_next):
    """HTML und statische Assets immer neu validieren, keine veralteten Stände im Browser."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
