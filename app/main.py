"""FastAPI-Backend: Dateibrowser, EKG-Analyse und Video-Streaming."""

from __future__ import annotations

import mimetypes
import os
import re
import threading

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import analysis, scp_reader
from .scp_reader import DEFAULT_FS, RecordInfo, discover_records, load_measurement

DATA_ROOT = os.environ.get("DATA_ROOT", "/data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}

app = FastAPI(title="gdmt-ekg")

_lock = threading.Lock()
_record_cache: dict[tuple[str, str], tuple[RecordInfo, object]] = {}
_analysis_cache: dict[str, dict] = {}


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
