"""FastAPI-Backend: Dateibrowser, EKG-Analyse und Video-Streaming."""

from __future__ import annotations

import mimetypes
import os
import re
import secrets
import shutil
import threading
import time

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
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


@app.get("/api/browse")
def browse(path: str = "") -> dict:
    """Verzeichnisinhalt für die Dateiauswahl auflisten."""
    full = _abs(path)
    if not os.path.isdir(full):
        raise HTTPException(status_code=404, detail="Ordner nicht gefunden")
    entries = []
    for name in sorted(os.listdir(full), key=str.lower):
        p = os.path.join(full, name)
        if os.path.isdir(p):
            entries.append({"name": name, "type": "dir", "path": _rel(p)})
        elif os.path.isfile(p):
            ext = os.path.splitext(name)[1].lower()
            entries.append(
                {
                    "name": name,
                    "type": "video" if ext in VIDEO_EXTS else "file",
                    "path": _rel(p),
                    "size": os.path.getsize(p),
                }
            )
    return {
        "path": _rel(full),
        "parent": _rel(os.path.dirname(full)) if _rel(full) else None,
        "entries": entries,
    }


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
def upload_ecg(files: list[UploadFile] = File(...)) -> dict:
    """SCP-Dateien (und optional README.TXT) als EKG-Datensatz hochladen."""
    session = secrets.token_hex(6)
    dest_dir = os.path.join(UPLOAD_DIR, f"e-{session}")
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
    if not any(s.lower().endswith(".scp") for s in saved):
        raise HTTPException(status_code=400, detail="Keine .scp-Dateien im Upload")
    return {"path": _rel(dest_dir), "files": sorted(saved)}


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
