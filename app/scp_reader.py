"""Lesen von SCP-Dateien des 'Easy ECG Monitor' (Creative Medical PC-80B).

Das Geraet schreibt ein SCP-ECG-aehnliches Format (EN-1064-Anlehnung):

- 0x00       u32  CRC /Praeambel
- 0x10       Magic "SCPECG"
- 0x18       u32  Laenge Sektion 0, u32 Anzahl Sektionseintraege
- 0x20       n * [id:u16][len:u32][off:u32]  (off ist 1-based!)
- Sektion    16-Byte-Header [crc:u32][len:u32][0x0d0d:u16][res:8]
- Sektion 6  EKG-Waveform: 8-Byte-Lead-Header, danach u16-Samples
- Sample     12-Bit-Amplitude; gesetztes hohes Nibble (0x8xxx) ist eine
             R-Zacken-Markierung des Geraets (vgl. Sektion 9).

Eine Messung ist laut README.TXT ueber mehrere SCP-Dateien verteilt
("No. / From / To"); die Samples laufen kontinuierlich fort.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"SCPECG"
DEFAULT_FS = 160.0
R_FLAG_NIBBLE = 0x8
BASELINE_AD = 2048.0  # 12-Bit-offset-binaer: Nulllinie des Signals


@dataclass
class ScpFile:
    """Dekodierte Inhalte einer einzelnen SCP-Datei."""

    path: str
    n_samples: int = 0
    signal: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    device_beats: list[int] = field(default_factory=list)


@dataclass
class Measurement:
    """Eine vollstaendige Messung (verkettete SCP-Dateien)."""

    files: list[str] = field(default_factory=list)
    signal: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    device_beats: list[int] = field(default_factory=list)
    fs: float = DEFAULT_FS

    @property
    def duration(self) -> float:
        return len(self.signal) / self.fs


def _sections(data: bytes) -> list[tuple[int, int, int]]:
    """Sektionsverzeichnis: [(id, len, off_0based), ...]."""
    i = data.find(MAGIC)
    if i < 0:
        raise ValueError("kein SCP-ECG-Format (Magic 'SCPECG' fehlt)")
    p = i + 16
    out = []
    while p + 10 <= len(data):
        sid, = struct.unpack_from("<H", data, p)
        slen, = struct.unpack_from("<I", data, p + 2)
        soff, = struct.unpack_from("<I", data, p + 6)
        p += 10
        start = soff - 1  # Offsets sind 1-based
        if slen == 0 or start < 0 or start + slen > len(data):
            break
        out.append((sid, slen, start))
    return out


def read_scp(path: str) -> ScpFile:
    """Einzelne SCP-Datei dekodieren (Signal + Geraete-R-Marken)."""
    with open(path, "rb") as fh:
        data = fh.read()

    waveform = [s for s in _sections(data) if s[0] == 6]
    if not waveform:
        raise ValueError(f"{os.path.basename(path)}: keine Waveform-Sektion (6) gefunden")
    _, slen, start = waveform[0]
    body = data[start + 16 : start + slen]
    lead_hdr = struct.unpack_from("<4H", body, 0)
    n_bytes = lead_hdr[3]
    raw = np.frombuffer(body, dtype="<u2", count=n_bytes // 2, offset=8)

    flagged = raw > 0x0FFF
    device_beats = np.flatnonzero(
        ((raw >> 12) & 0xF) == R_FLAG_NIBBLE
    ).tolist()
    amplitude = (raw & 0x0FFF).astype(np.float32) - BASELINE_AD
    return ScpFile(
        path=path,
        n_samples=len(raw),
        signal=amplitude,
        device_beats=device_beats,
    )


def _natural_key(name: str) -> list[int | str]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def scp_number(name: str) -> int | None:
    m = re.fullmatch(r"(\d+)\.scp", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


@dataclass
class RecordInfo:
    """Ein Datensatz laut README.TXT (From..To) bzw. Ordner-Fallback."""

    no: int
    label: str
    files: list[str] = field(default_factory=list)
    source: str = "readme"  # readme | folder


def parse_readme(path: str) -> list[tuple[int, str, str]]:
    """README.TXT auswerten -> [(no, from_name, to_name), ...]."""
    rows: list[tuple[int, str, str]] = []
    with open(path, encoding="latin-1", errors="ignore") as fh:
        for line in fh:
            m = re.match(
                r"^\s*(\d+)\s+(\d+\.scp)\s+(\d+\.scp)\s*$", line.strip(), re.I
            )
            if m:
                rows.append((int(m.group(1)), m.group(2), m.group(3)))
    return rows


def _collect_scps(root: str) -> dict[str, list[str]]:
    """Alle SCP-Dateien unterhalb von root, gruppiert nach Ordner (rekursiv)."""
    index: dict[str, list[str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        scps = [f for f in filenames if f.lower().endswith(".scp")]
        if scps:
            scps.sort(key=_natural_key)
            index[dirpath] = [os.path.join(dirpath, f) for f in scps]
    return index


def _resolve_range(
    from_no: int, to_no: int, index: dict[str, list[str]]
) -> list[str]:
    """Dateinamen From..To in einem der gescannten Ordner aufloesen."""
    numbers = list(range(from_no, to_no + 1))
    for _dir, files in sorted(index.items()):
        by_no = {}
        for f in files:
            no = scp_number(os.path.basename(f))
            if no is not None:
                by_no[no] = f
        if all(no in by_no for no in numbers):
            return [by_no[no] for no in numbers]
    return []


def discover_records(folder: str) -> list[RecordInfo]:
    """Messungen eines Ordners ermitteln.

    1. README.TXT im Ordner (oder im uebergeordneten Ordner) definiert
       Messungen als Bereich "from.scp..to.scp".
    2. Fallback: jeder Ordner mit SCP-Dateien bildet eine Messung.
    """
    index = _collect_scps(folder)
    readme = None
    for cand in (
        os.path.join(folder, "README.TXT"),
        os.path.join(os.path.dirname(folder.rstrip(os.sep)), "README.TXT"),
    ):
        if os.path.isfile(cand):
            readme = cand
            break

    records: list[RecordInfo] = []
    if readme:
        for no, from_name, to_name in parse_readme(readme):
            files = _resolve_range(
                scp_number(from_name), scp_number(to_name), index
            )
            if files:
                records.append(
                    RecordInfo(
                        no=no,
                        label=f"Messung {no} ({os.path.basename(from_name)}\u2013"
                        f"{os.path.basename(to_name)})",
                        files=files,
                    )
                )

    if not records:
        for _dir, files in sorted(index.items()):
            records.append(
                RecordInfo(
                    no=len(records) + 1,
                    label=f"{os.path.basename(_dir) or _dir} "
                    f"({len(files)} SCP-Dateien)",
                    files=files,
                    source="folder",
                )
            )
    return records


def load_measurement(record: RecordInfo, fs: float = DEFAULT_FS) -> Measurement:
    """Alle SCP-Dateien eines Datensatzes zu einem Signal verketten."""
    signals: list[np.ndarray] = []
    device_beats: list[int] = []
    files: list[str] = []
    offset = 0
    for path in record.files:
        scp = read_scp(path)
        signals.append(scp.signal)
        device_beats.extend(b + offset for b in scp.device_beats)
        offset += scp.n_samples
        files.append(path)
    signal = np.concatenate(signals) if signals else np.empty(0, dtype=np.float32)
    return Measurement(files=files, signal=signal, device_beats=device_beats, fs=fs)
