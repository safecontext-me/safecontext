"""Call recordings and meeting transcripts that already exist on this Mac.

This reader only ingests what is already on disk. It never records a call:
recording one raises consent law (two-party consent states), so capture is out of
scope. Audio and transcripts never leave the Mac; audio without a transcript is
transcribed on-device by an optional engine (see "Transcription" below).

Inputs, their paths and the permission each needs
=================================================

Watched folder (``scan_folder``)
    Any folder the user picks. Audio (.m4a .mp3 .wav .aiff .caf .mp4 .mov .aac
    .flac) is paired with a transcript that shares its stem (``call.m4a`` +
    ``call.vtt``). Transcripts with no audio are recordings in their own right.
    Permission: none for most folders. ~/Documents, ~/Desktop and ~/Downloads
    trigger macOS's one-time "access files in your Documents folder" prompt
    (TCC); iCloud Drive and external volumes have prompts of their own. Picking
    the folder in an NSOpenPanel grants access for that run, so the UI should use
    the system folder picker rather than a typed path.

Zoom local recordings (``scan_zoom``), ``~/Documents/Zoom``
    One subfolder per meeting, named ``YYYY-MM-DD HH.MM.SS <Topic> [<id>]``. In
    it: the mixed audio (``audio_only.m4a`` in older clients, ``audio<digits>.m4a``
    in newer ones), the video (``zoom_0.mp4`` / ``video<digits>.mp4``), the saved
    chat (``meeting_saved_chat.txt`` / ``chat.txt``), saved captions
    (``closed_caption.txt``, lines like ``[Name] 14:02:11 text``) and, when "record
    a separate audio file for each participant" is on, an ``Audio Record`` folder
    of ``audio<Name><digits>.m4a`` tracks. Local recordings carry no transcript;
    Zoom only transcribes cloud recordings, whose download is a ``.vtt`` with
    ``Name: text`` cues, picked up if the user drops it in the meeting folder.
    Participants come from the per-person tracks, the chat and the captions.
    Permission: the Documents-folder prompt above.

macOS Voice Memos (``scan_voice_memos``)
    macOS 14+: ``~/Library/Group Containers/group.com.apple.VoiceMemos.shared/
    Recordings`` (``*.m4a`` plus ``CloudRecordings.db``, a Core Data SQLite store:
    table ``ZCLOUDRECORDING`` with ``ZDATE`` (seconds since 2001-01-01),
    ``ZDURATION``, ``ZENCRYPTEDTITLE`` (the plain title, despite the name),
    ``ZCUSTOMLABEL``, ``ZPATH``, ``ZUNIQUEID``). macOS 13 and earlier:
    ``~/Library/Application Support/com.apple.voicememos/Recordings``. macOS 15
    (iOS 18) Voice Memos stores its own transcript inside the .m4a as a ``tsrp``
    MP4 box holding JSON (``attributedString.runs`` alternating text and an index
    into ``attributeTable`` of ``timeRange`` pairs); that format is reverse
    engineered, so it is parsed best-effort and the audio is transcribed when it
    is missing. Permission: the container belongs to another app, so reading it
    needs Full Disk Access (System Settings > Privacy & Security > Full Disk
    Access). Without it listing fails with EPERM; ``check_access`` reports
    ``"denied"`` and the UI should explain how to grant it. It cannot be
    requested programmatically. Not present at all when Voice Memos was never
    opened (and on the Mac used to write this, neither path existed).

Transcript files (any of the above, or a watched folder)
    ``.vtt``: Zoom cloud (``Name: text`` cues) and Microsoft Teams (``<v Name>``
    voice tags). ``.srt``: Otter, Descript, Rev. ``.txt``/``.md``/``.docx``:
    Otter (``Name  0:00`` header line, then the text), Teams .docx (same shape),
    Google Meet (a Google Doc exported from the "Meet Recordings" Drive folder:
    title ``<Meeting> - Transcript``, an ``Attendees`` line, ``00:00:00`` lines
    before blocks of ``Name: text``), Granola (Markdown copy: ``**Me:** text`` /
    ``Them: text``, usually without times) and plain ``[00:01:02] Name: text``.
    .docx is read with ``zipfile`` (no python-docx). A .txt/.md/.docx counts as a
    transcript only if it has timestamps or speaker labels, or its name contains
    "transcript", so ordinary notes in a watched folder are skipped.

Transcription
=============

Engine comparison (Nov 2026; the app is a universal Briefcase build, which takes
wheels only and merges the arm64 and x86_64 wheels of the same version):

* whisper.cpp via ``pywhispercpp`` (chosen). Same Whisper models, so the same
  accuracy as the others. Metal on Apple Silicon, AVX/Accelerate on Intel.
  Measured on an M5 Pro: a 5.6 s clip in 0.35 s with ``base.en`` after a ~8 s
  first load (Metal shader compile). Wheel ~4 MB (+ numpy, which pywhispercpp
  imports). Catch: 1.5.x ships arm64 wheels only; the last x86_64/universal2
  wheel is 1.2.0. For the universal app, build a 1.5.x x86_64 wheel into
  packaging/wheels (like proxy_tools) or pin ``pywhispercpp==1.2.0``. It
  needs ffmpeg for anything but WAV, so this module decodes with macOS's own
  ``afconvert`` instead. Models are single ggml files from Hugging Face.
* ``mlx-whisper``: fastest on Apple Silicon, but MLX has no Intel wheels at all,
  so a universal build cannot include it; pulls mlx + mlx-metal + numpy and
  wants ffmpeg. Would suit an Apple-Silicon-only build.
* ``faster-whisper``: CTranslate2, CPU only on macOS (no Metal), so several times
  slower than whisper.cpp on Apple Silicon. Wheels exist for both arches, but
  the stack (ctranslate2, av with its bundled ffmpeg, onnxruntime for VAD,
  tokenizers, huggingface_hub) is ~60 MB of wheels per arch, ~130 MB+ merged.
* Apple's Speech framework via pyobjc (``SFSpeechRecognizer`` with
  ``requiresOnDeviceRecognition``): nothing to download or bundle (beyond
  pyobjc-framework-Speech) and runs on both arches, but accuracy and punctuation
  trail Whisper on long, multi-speaker calls, it needs the Speech Recognition
  permission (``NSSpeechRecognitionUsageDescription`` in Info.plist plus a TCC
  prompt) and on-device language assets the user may not have. macOS 26's better
  ``SpeechAnalyzer``/``SpeechTranscriber`` is Swift-only, out of pyobjc's reach.
  A reasonable zero-download fallback later; it fits behind the same interface.

Models are downloaded on first use, with progress, into
``~/Library/Application Support/SafeContext/models/whisper`` and checked against
a pinned SHA-256; they are never bundled. The download fetches a public model
file and sends nothing about the user. Defaults: ``large-v3-turbo-q5_0`` (547 MB,
near large-v3 accuracy) on Apple Silicon, ``base`` (148 MB) on Intel, where the
large model runs slower than real time.

Speaker labels: kept whenever the source has them (VTT voice tags, ``Name:``
lines, Zoom's per-participant tracks, which are transcribed one by one and
merged, so they come out labelled by name). Diarizing a single mixed track
on-device is not practical yet: pyannote needs PyTorch (hundreds of MB) and
gated models; whisper.cpp's tinydiarize only marks speaker turns, English only,
with one ``small.en-tdrz`` model; sherpa-onnx diarization is the most plausible
later option (~40 MB of ONNX models, onnxruntime).

Incremental sync
================

``Cursor`` maps each file path to (mtime_ns, size, content hash) and the
recording it belongs to. A scan returns only recordings whose files are new or
changed (a touched file with the same hash is not re-emitted) plus the ids of
recordings whose files disappeared. The hash covers the size and the first and
last 1 MiB, so multi-GB videos are not read end to end. Save ``ScanResult.cursor``
only after the caller has stored those recordings.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import urllib.request
import wave
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol
from xml.etree import ElementTree

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aiff", ".aif", ".caf", ".aac", ".flac",
              ".mp4", ".mov", ".m4v", ".qta"}
TRANSCRIPT_EXTS = {".vtt", ".srt", ".txt", ".md", ".markdown", ".docx"}
_STRUCTURED_TRANSCRIPTS = {".vtt", ".srt"}

SOURCE_FOLDER = "folder"
SOURCE_ZOOM = "zoom"
SOURCE_VOICE_MEMOS = "voice_memos"

# Progress callbacks get (done, total); total is 0 when unknown.
Progress = Callable[[int, int], None]


def default_zoom_dir() -> Path:
    return Path.home() / "Documents" / "Zoom"


def default_voice_memos_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Library" / "Group Containers" / "group.com.apple.VoiceMemos.shared" / "Recordings",
        home / "Library" / "Application Support" / "com.apple.voicememos" / "Recordings",
    ]


def default_models_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "SafeContext" / "models" / "whisper"


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Segment:
    start: Optional[float]  # seconds from the start of the recording; None if unknown
    end: Optional[float]
    text: str
    speaker: Optional[str] = None


@dataclass
class Recording:
    id: str
    source: str                      # SOURCE_FOLDER / SOURCE_ZOOM / SOURCE_VOICE_MEMOS
    title: str
    start_time: Optional[datetime]
    duration: Optional[float]        # seconds
    participants: list[str]
    path: Path                       # the main file: the audio if any, else the transcript
    audio_path: Optional[Path] = None
    transcript_path: Optional[Path] = None
    transcript_format: Optional[str] = None  # "vtt", "srt", "text", "docx", "voice_memos", "whisper"
    segments: list[Segment] = field(default_factory=list)
    # Per-speaker audio (Zoom "separate audio file for each participant"): name -> file.
    speaker_tracks: dict[str, Path] = field(default_factory=dict)

    @property
    def needs_transcription(self) -> bool:
        return not self.segments and self.audio_path is not None

    @property
    def text(self) -> str:
        lines = []
        for s in self.segments:
            lines.append(f"{s.speaker}: {s.text}" if s.speaker else s.text)
        return "\n".join(lines)

    def files(self) -> list[Path]:
        out = [p for p in (self.audio_path, self.transcript_path) if p is not None]
        out += list(self.speaker_tracks.values())
        if not out:
            out.append(self.path)
        return out


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int
    digest: str
    recording_id: str


@dataclass
class Cursor:
    files: dict[str, FileState] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "files": {k: asdict(v) for k, v in self.files.items()}})

    @classmethod
    def from_json(cls, data: str | None) -> "Cursor":
        if not data:
            return cls()
        raw = json.loads(data)
        return cls({k: FileState(**v) for k, v in raw.get("files", {}).items()})


@dataclass
class ScanResult:
    recordings: list[Recording]      # new or changed since the cursor
    removed: list[str]               # ids of recordings whose files are gone
    cursor: Cursor                   # save this once the recordings are stored
    errors: list[str] = field(default_factory=list)  # files skipped, with the reason


# --------------------------------------------------------------------------- access


def check_access(path: Path) -> str:
    """"ok", "missing" or "denied" (EPERM: needs a TCC grant such as Full Disk Access)."""
    path = Path(path)
    try:
        if not path.exists():
            return "missing"
        if path.is_dir():
            os.listdir(path)
        else:
            with open(path, "rb"):
                pass
        return "ok"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "missing"


# --------------------------------------------------------------------------- helpers


def _recording_id(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}:{key}".encode()).hexdigest()[:20]


def _quick_hash(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    with open(path, "rb") as f:
        if size <= 2 * chunk:
            h.update(f.read())
        else:
            h.update(f.read(chunk))
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return h.hexdigest()


_TS_RE = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"


def _parse_ts(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    secs = float(parts[-1])
    mult = 60
    for p in reversed(parts[:-1]):
        secs += int(p) * mult
        mult *= 60
    return secs


_FILENAME_DATE_RES = [
    # Zoom cloud: GMT20240501-143000_Recording ; generic 2024-05-01 14.30.00 / 2024-05-01_14-30
    (re.compile(r"GMT(\d{8})-(\d{6})"), lambda m: datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)),
    (re.compile(r"(\d{4}-\d{2}-\d{2})[ _T](\d{2})[.:\-](\d{2})(?:[.:\-](\d{2}))?"),
     lambda m: datetime.strptime(f"{m[1]} {m[2]}:{m[3]}:{m[4] or '00'}", "%Y-%m-%d %H:%M:%S").astimezone()),
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), lambda m: datetime.strptime(m[1], "%Y-%m-%d").astimezone()),
]


def _date_from_name(name: str) -> Optional[datetime]:
    for rx, conv in _FILENAME_DATE_RES:
        m = rx.search(name)
        if m:
            try:
                return conv(m)
            except ValueError:
                continue
    return None


def _file_birth(path: Path) -> Optional[datetime]:
    try:
        st = path.stat()
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _fill_ends(segs: list[Segment], duration: Optional[float] = None) -> list[Segment]:
    out = []
    for i, s in enumerate(segs):
        end = s.end
        if end is None and s.start is not None:
            nxt = next((t.start for t in segs[i + 1:] if t.start is not None), None)
            end = nxt if nxt is not None else duration
        out.append(replace(s, end=end))
    return out


def _speakers(segs: Iterable[Segment]) -> list[str]:
    seen: dict[str, None] = {}
    for s in segs:
        if s.speaker and s.speaker.lower() not in {"me", "them", "unknown"} \
                and not re.fullmatch(r"speaker \d+", s.speaker.lower()):
            seen.setdefault(s.speaker, None)
    return list(seen)


# --------------------------------------------------------------------------- transcript parsers

_TAG_RE = re.compile(r"<[^>]+>")
_VOICE_RE = re.compile(r"<v(?:\.[\w.]+)?\s+([^>]+)>")
_NAME = r"(?:Speaker \d+|Me|Them|[A-Z\u00C0-\u024F][\w.'\u2019\-]*(?: [\w\u00C0-\u024F][\w.'\u2019\-]*){0,4})"
_NAME_TEXT_RE = re.compile(rf"^(?P<name>{_NAME})\s*:\s+(?P<text>.+)$")


def _split_name(text: str) -> tuple[Optional[str], str]:
    m = _NAME_TEXT_RE.match(text)
    if m:
        return m["name"].strip(), m["text"].strip()
    return None, text


def parse_vtt(content: str) -> list[Segment]:
    """WebVTT (Zoom cloud ``Name: text`` cues, Teams ``<v Name>`` tags) and SRT."""
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    segs: list[Segment] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue  # WEBVTT header, NOTE, STYLE
        start_s, _, rest = lines[idx].partition("-->")
        end_s = rest.strip().split()[0] if rest.strip() else ""
        try:
            start, end = _parse_ts(start_s), _parse_ts(end_s)
        except ValueError:
            continue
        raw = " ".join(lines[idx + 1:])
        speaker = None
        vm = _VOICE_RE.search(raw)
        if vm:
            speaker = vm[1].strip()
        text = html.unescape(_TAG_RE.sub("", raw)).strip()
        if speaker is None:
            speaker, text = _split_name(text)
        if not text:
            continue
        # Teams splits one utterance over several cues; join them back.
        if segs and segs[-1].speaker == speaker and speaker is not None \
                and segs[-1].end is not None and start - segs[-1].end < 0.5 \
                and not segs[-1].text.endswith((".", "?", "!")):
            prev = segs.pop()
            segs.append(Segment(prev.start, end, f"{prev.text} {text}", speaker))
        else:
            segs.append(Segment(start, end, text, speaker))
    return segs


parse_srt = parse_vtt

_TS_ONLY_RE = re.compile(rf"^[\[(]?(?P<ts>{_TS_RE})[\])]?$")
_HEADER_RE = re.compile(rf"^(?P<name>{_NAME}|[^\d\s:\[\]][^:\[\]]{{0,60}}?)\s+[\[(]?(?P<ts>{_TS_RE})[\])]?$")
_TS_LINE_RE = re.compile(rf"^[\[(](?P<ts>{_TS_RE})[\])]\s*[-\u2013]?\s*(?P<rest>.+)$|^(?P<ts2>{_TS_RE})\s+[-\u2013]?\s*(?P<rest2>.+)$")
_CAPTION_RE = re.compile(rf"^\[(?P<name>[^\]]+)\]\s+(?P<ts>{_TS_RE})\s+(?P<text>.+)$")
_SKIP_LINE_RE = re.compile(r"^(transcription ended after|this editable transcript was computer generated|"
                           r"transcribed by https?://otter\.ai|webvtt)", re.I)


@dataclass
class _ParsedText:
    segments: list[Segment]
    title: Optional[str] = None
    participants: list[str] = field(default_factory=list)
    timed: bool = False
    labelled: bool = False


def parse_text_transcript(content: str) -> _ParsedText:
    """Plain-text and Markdown transcripts from Otter, Teams, Google Meet, Granola, Zoom captions."""
    out = _ParsedText([])
    pending_ts: Optional[float] = None
    cur: Optional[dict] = None
    caption_base: Optional[float] = None
    attendees_next = False

    def flush():
        nonlocal cur
        if cur and cur["text"]:
            out.segments.append(Segment(cur["start"], None, " ".join(cur["text"]).strip(), cur["speaker"]))
        cur = None

    def start(speaker, ts, text=""):
        nonlocal cur, pending_ts
        flush()
        if ts is None:
            ts, pending_ts = pending_ts, None
        cur = {"speaker": speaker, "start": ts, "text": [text] if text else []}

    for raw in content.replace("\r\n", "\n").lstrip("\ufeff").split("\n"):
        line = raw.strip()
        line = re.sub(r"^[-*+]\s+", "", line)           # bullets
        line = line.replace("**", "").replace("__", "").strip()
        if not line:
            continue
        if _SKIP_LINE_RE.match(line):
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if out.title is None and heading.lower() not in {"transcript", "notes"}:
                out.title = heading
            continue
        if attendees_next:
            attendees_next = False
            out.participants = [p.strip() for p in re.split(r",|;", line) if p.strip()]
            continue
        low = line.lower()
        if low in {"attendees", "participants"}:
            attendees_next = True
            continue
        m = re.match(r"^(attendees|participants)\s*:\s*(.+)$", line, re.I)
        if m:
            out.participants = [p.strip() for p in re.split(r",|;", m[2]) if p.strip()]
            continue
        if low == "transcript":
            continue
        if out.title is None and not out.segments and cur is None and low.endswith("- transcript"):
            out.title = line[: -len("- transcript")].strip(" -")
            continue

        if m := _TS_ONLY_RE.match(line):
            flush()
            pending_ts = _parse_ts(m["ts"])
            out.timed = True
            continue
        if m := _CAPTION_RE.match(line):          # Zoom saved captions: [Name] 14:02:11 text
            t = _parse_ts(m["ts"])
            caption_base = t if caption_base is None else caption_base
            start(m["name"].strip(), t - caption_base, m["text"].strip())
            out.timed = out.labelled = True
            continue
        if (m := _HEADER_RE.match(line)) and not _NAME_TEXT_RE.match(line):  # Otter / Teams docx
            start(m["name"].strip(), _parse_ts(m["ts"]))
            out.timed = out.labelled = True
            continue
        if m := _TS_LINE_RE.match(line):          # [00:01:02] Name: text
            ts = m["ts"] or m["ts2"]
            rest = (m["rest"] or m["rest2"]).strip()
            speaker, text = _split_name(rest)
            start(speaker, _parse_ts(ts), text)
            out.timed = True
            out.labelled |= speaker is not None
            continue
        speaker, text = _split_name(line)
        if speaker is not None:                   # Name: text (Meet, Granola)
            start(speaker, None, text)
            out.labelled = True
            continue
        if cur is None:
            start(None, None, line)
        else:
            cur["text"].append(line)
    flush()
    return out


def read_docx(path: Path) -> tuple[str, Optional[str], Optional[datetime]]:
    """The text of a .docx (one line per paragraph), its title and creation date."""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    W = "{%s}" % ns["w"]
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
        core = z.read("docProps/core.xml") if "docProps/core.xml" in z.namelist() else None
    paras = []
    for p in root.iter(W + "p"):
        bits = []
        for el in p.iter():
            if el.tag == W + "t" and el.text:
                bits.append(el.text)
            elif el.tag == W + "tab":
                bits.append("\t")
            elif el.tag in (W + "br", W + "cr"):
                bits.append("\n")
        paras.append("".join(bits))
    title = created = None
    if core:
        croot = ElementTree.fromstring(core)
        for el in croot:
            tag = el.tag.split("}")[-1]
            if tag == "title" and el.text:
                title = el.text.strip()
            elif tag == "created" and el.text:
                try:
                    created = datetime.fromisoformat(el.text.strip().replace("Z", "+00:00"))
                except ValueError:
                    pass
    return "\n".join(paras), title, created


@dataclass
class TranscriptFile:
    segments: list[Segment]
    format: str
    title: Optional[str] = None
    participants: list[str] = field(default_factory=list)
    start_time: Optional[datetime] = None
    looks_like_transcript: bool = True


def read_transcript(path: Path) -> TranscriptFile:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in _STRUCTURED_TRANSCRIPTS:
        segs = parse_vtt(path.read_text(encoding="utf-8", errors="replace"))
        return TranscriptFile(segs, ext[1:], participants=_speakers(segs),
                              looks_like_transcript=bool(segs))
    created = None
    doc_title = None
    if ext == ".docx":
        content, doc_title, created = read_docx(path)
        fmt = "docx"
    else:
        content = path.read_text(encoding="utf-8", errors="replace")
        fmt = "text"
    parsed = parse_text_transcript(content)
    segs = _fill_ends(parsed.segments)
    participants = parsed.participants or _speakers(segs)
    looks = bool(segs) and (parsed.timed or parsed.labelled or "transcript" in path.name.lower())
    return TranscriptFile(segs, fmt, parsed.title or doc_title, participants, created, looks)


# --------------------------------------------------------------------------- audio metadata

_MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta", b"ilst", b"edts"}
_MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def _iter_boxes(buf: bytes, start: int = 0, end: Optional[int] = None):
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        hdr = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            return
        yield typ, i + hdr, i + size
        i += size


def _read_moov(path: Path) -> Optional[bytes]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return None
            size, typ = struct.unpack(">I4s", hdr[:8])
            if size == 1:
                size = struct.unpack(">Q", hdr[8:16])[0]
            elif size == 0:
                size = total - pos
            if size < 8:
                return None
            if typ == b"moov":
                if size > 256 << 20:
                    return None
                f.seek(pos)
                return f.read(size)
            pos += size
    return None


def _find_box(buf: bytes, want: bytes, start: int = 0, end: Optional[int] = None):
    for typ, s, e in _iter_boxes(buf, start, end):
        if typ == want:
            return s, e
        if typ in _MP4_CONTAINERS:
            inner = s
            if typ == b"meta" and buf[s + 4:s + 8] not in (b"hdlr", b"keys", b"ilst"):
                inner = s + 4  # ISO full box: skip version/flags
            hit = _find_box(buf, want, inner, e)
            if hit:
                return hit
    return None


@dataclass
class AudioInfo:
    duration: Optional[float] = None
    created: Optional[datetime] = None
    embedded_transcript: Optional[list[Segment]] = None


def audio_info(path: Path) -> AudioInfo:
    """Duration, creation time and (Voice Memos) embedded transcript, without decoding audio."""
    path = Path(path)
    ext = path.suffix.lower()
    info = AudioInfo()
    try:
        if ext == ".wav":
            with wave.open(str(path), "rb") as w:
                info.duration = w.getnframes() / float(w.getframerate())
            return info
        if ext in {".m4a", ".mp4", ".mov", ".m4v", ".qta", ".aac"}:
            moov = _read_moov(path)
            if moov is None:
                return info
            body = (8, len(moov))
            mvhd = _find_box(moov, b"mvhd", *body)
            if mvhd:
                s, _ = mvhd
                version = moov[s]
                if version == 1:
                    created, _, scale, dur = struct.unpack(">QQIQ", moov[s + 4:s + 32])
                else:
                    created, _, scale, dur = struct.unpack(">IIII", moov[s + 4:s + 20])
                if scale:
                    info.duration = dur / scale
                if created:
                    info.created = _MAC_EPOCH + timedelta(seconds=created)
            tsrp = _find_box(moov, b"tsrp", *body)
            if tsrp:
                info.embedded_transcript = parse_voice_memo_transcript(moov[tsrp[0]:tsrp[1]])
    except (OSError, struct.error, wave.Error, EOFError):
        pass
    return info


def parse_voice_memo_transcript(blob: bytes) -> Optional[list[Segment]]:
    """Voice Memos' ``tsrp`` JSON: word runs with time ranges, grouped into sentences."""
    try:
        data = json.loads(blob.decode("utf-8"))
        attr = data["attributedString"]
        runs, table = attr["runs"], attr["attributeTable"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    words: list[tuple[str, float, float]] = []
    for i in range(0, len(runs) - 1, 2):
        text, idx = runs[i], runs[i + 1]
        if not isinstance(text, str) or not isinstance(idx, int) or idx >= len(table):
            continue
        tr = table[idx].get("timeRange") if isinstance(table[idx], dict) else None
        if not tr or len(tr) != 2:
            continue
        words.append((text, float(tr[0]), float(tr[1])))
    segs: list[Segment] = []
    buf: list[tuple[str, float, float]] = []

    def flush():
        if buf:
            segs.append(Segment(buf[0][1], buf[-1][2], "".join(w for w, _, _ in buf).strip()))
            buf.clear()

    for w in words:
        if buf and (w[1] - buf[-1][2] > 1.5):
            flush()
        buf.append(w)
        if w[0].rstrip().endswith((".", "?", "!")) and len(buf) >= 4:
            flush()
    flush()
    return [s for s in segs if s.text] or None


# --------------------------------------------------------------------------- building recordings


def _build(source: str, key: str, title: str, audio: Optional[Path], transcript: Optional[Path],
           start_hint: Optional[datetime] = None) -> Recording:
    info = audio_info(audio) if audio else AudioInfo()
    tf = read_transcript(transcript) if transcript else None
    segs: list[Segment] = []
    fmt = None
    participants: list[str] = []
    if tf and tf.segments:
        segs, fmt, participants = tf.segments, tf.format, list(tf.participants)
    elif info.embedded_transcript:
        segs, fmt = info.embedded_transcript, "voice_memos"
    duration = info.duration
    if duration is None and segs and segs[-1].end is not None:
        duration = segs[-1].end
    segs = _fill_ends(segs, duration)
    name_src = (audio or transcript).name
    start = (start_hint or _date_from_name(name_src) or (tf.start_time if tf else None)
             or info.created or _file_birth(audio or transcript))
    return Recording(
        id=_recording_id(source, key),
        source=source,
        title=(tf.title if tf and tf.title else None) or title,
        start_time=start,
        duration=duration,
        participants=participants,
        path=audio or transcript,
        audio_path=audio,
        transcript_path=transcript,
        transcript_format=fmt,
        segments=segs,
    )


def _diff(candidates: list[tuple[Recording, list[Path]]], cursor: Optional[Cursor],
          scope: Iterable[Path] = ()) -> ScanResult:
    """Keep the recordings whose files changed since ``cursor``.

    ``candidates`` pairs a lazily built recording thunk's result with its files.
    Paths in the old cursor under any of ``scope`` that were not seen are removed.
    """
    old = cursor.files if cursor else {}
    new: dict[str, FileState] = {k: v for k, v in old.items()}
    seen: set[str] = set()
    changed: list[Recording] = []
    for rec, files in candidates:
        rec_changed = False
        for p in files:
            key = str(p)
            seen.add(key)
            st = p.stat()
            prev = old.get(key)
            if prev and prev.mtime_ns == st.st_mtime_ns and prev.size == st.st_size \
                    and prev.recording_id == rec.id:
                continue
            digest = _quick_hash(p)
            if not (prev and prev.digest == digest and prev.recording_id == rec.id):
                rec_changed = True
            new[key] = FileState(st.st_mtime_ns, st.st_size, digest, rec.id)
        if rec_changed:
            changed.append(rec)
    roots = [str(Path(s)) for s in scope]
    gone_ids: set[str] = set()
    for key, state in old.items():
        if key in seen or not any(key == r or key.startswith(r.rstrip("/") + "/") for r in roots):
            continue
        del new[key]
        gone_ids.add(state.recording_id)
    live_ids = {s.recording_id for s in new.values()}
    removed = sorted(gone_ids - live_ids)
    return ScanResult(changed, removed, Cursor(new))


def _group_folder(paths: list[Path]) -> list[tuple[Optional[Path], Optional[Path]]]:
    """Pair audio with a transcript of the same stem, in one directory."""
    by_stem: dict[tuple[Path, str], dict[str, list[Path]]] = {}
    for p in paths:
        ext = p.suffix.lower()
        kind = "audio" if ext in AUDIO_EXTS else "transcript" if ext in TRANSCRIPT_EXTS else None
        if kind is None:
            continue
        stem = p.stem
        # Zoom cloud: "GMT..._Recording.transcript.vtt" next to "GMT..._Recording.m4a"
        for suffix in (".transcript", ".cc", "_transcript", " transcript", " - Transcript"):
            if stem.lower().endswith(suffix.lower()):
                stem = stem[: -len(suffix)]
        by_stem.setdefault((p.parent, stem), {"audio": [], "transcript": []})[kind].append(p)
    pairs: list[tuple[Optional[Path], Optional[Path]]] = []
    rank = {".vtt": 0, ".srt": 1, ".docx": 2, ".md": 3, ".markdown": 3, ".txt": 4}
    for _, group in sorted(by_stem.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        audios = sorted(group["audio"], key=lambda p: (p.suffix.lower() in {".mp4", ".mov", ".m4v"}, p.name))
        trans = sorted(group["transcript"], key=lambda p: (rank.get(p.suffix.lower(), 9), p.name))
        audio = audios[0] if audios else None
        if audio is None:
            for t in trans:
                pairs.append((None, t))
        else:
            pairs.append((audio, trans[0] if trans else None))
            pairs.extend((a, None) for a in audios[1:]
                         if a.suffix.lower() not in {".mp4", ".mov", ".m4v"})
    return pairs


def _walk(root: Path, max_depth: int = 6) -> list[Path]:
    out: list[Path] = []
    root = Path(root)
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if not n.startswith(".") and not n.endswith((".app", ".photoslibrary"))]
        if len(d.parts) - base_depth >= max_depth:
            dirnames[:] = []
        out.extend(d / f for f in filenames if not f.startswith("."))
    return out


def scan_folder(folder: Path, cursor: Optional[Cursor] = None, *, source: str = SOURCE_FOLDER) -> ScanResult:
    """Recordings and transcripts anywhere under a folder the user picked."""
    folder = Path(folder).expanduser()
    errors: list[str] = []
    cands: list[tuple[Recording, list[Path]]] = []
    for audio, transcript in _group_folder(_walk(folder)):
        main = audio or transcript
        try:
            if audio is None and transcript.suffix.lower() not in _STRUCTURED_TRANSCRIPTS:
                if not read_transcript(transcript).looks_like_transcript:
                    continue
            rec = _build(source, str(main), main.stem, audio, transcript)
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
            errors.append(f"{main}: {type(e).__name__}: {e}")
            continue
        cands.append((rec, rec.files()))
    res = _diff(cands, cursor, [folder])
    res.errors = errors
    return res


_ZOOM_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2})\.(\d{2})\.(\d{2})\s*(.*?)(?:\s+(\d{9,12}))?$")
_ZOOM_CHAT_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}\s+(?:From\s+)?(.+?)\s+(?:to|To)\s+.+?:")
_ZOOM_CHAT_NEW_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}\s+(?:From\s+)?(.+?)\s*:\s")


def _zoom_participant_from_track(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"^audio", "", stem)
    stem = re.sub(r"\d{6,}$", "", stem).strip(" _-")
    return stem


def scan_zoom(root: Optional[Path] = None, cursor: Optional[Cursor] = None) -> ScanResult:
    """Zoom local recordings: one recording per meeting folder."""
    root = Path(root) if root else default_zoom_dir()
    errors: list[str] = []
    cands: list[tuple[Recording, list[Path]]] = []
    if not root.is_dir():
        return _diff([], cursor, [root])
    for meeting in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            files = [p for p in meeting.iterdir() if p.is_file() and not p.name.startswith(".")]
            m = _ZOOM_DIR_RE.match(meeting.name)
            start = title = None
            if m:
                start = datetime.strptime(f"{m[1]} {m[2]}:{m[3]}:{m[4]}", "%Y-%m-%d %H:%M:%S").astimezone()
                title = m[5] or "Zoom meeting"
            audios = sorted((p for p in files if p.suffix.lower() == ".m4a"),
                            key=lambda p: (not p.name.lower().startswith("audio"), p.name))
            videos = sorted(p for p in files if p.suffix.lower() in {".mp4", ".mov"})
            audio = audios[0] if audios else (videos[0] if videos else None)
            vtts = sorted((p for p in files if p.suffix.lower() in {".vtt", ".srt"}),
                          key=lambda p: ("transcript" not in p.name.lower(), p.name))
            captions = [p for p in files if p.name.lower() in {"closed_caption.txt", "closed_captions.txt"}]
            transcript = vtts[0] if vtts else (captions[0] if captions else None)
            if audio is None and transcript is None:
                continue
            rec = _build(SOURCE_ZOOM, str(meeting), title or meeting.name, audio, transcript, start)
            participants = list(rec.participants)
            tracks_dir = meeting / "Audio Record"
            if tracks_dir.is_dir():
                for t in sorted(tracks_dir.iterdir()):
                    if t.suffix.lower() == ".m4a":
                        who = _zoom_participant_from_track(t.name) or t.stem
                        rec.speaker_tracks[who] = t
                        participants.append(who)
            for chat in (p for p in files if p.name.lower() in {"meeting_saved_chat.txt", "chat.txt"}):
                for line in chat.read_text(encoding="utf-8", errors="replace").splitlines():
                    cm = _ZOOM_CHAT_RE.match(line) or _ZOOM_CHAT_NEW_RE.match(line)
                    if cm:
                        participants.append(cm[1].strip())
            rec.participants = list(dict.fromkeys(p for p in participants if p))
            extra = [p for p in files if p.name.lower() in {"meeting_saved_chat.txt", "chat.txt"}]
            cands.append((rec, rec.files() + extra))
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as e:
            errors.append(f"{meeting}: {type(e).__name__}: {e}")
    res = _diff(cands, cursor, [root])
    res.errors = errors
    return res


_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _voice_memo_rows(db: Path) -> dict[str, dict]:
    """Rows of CloudRecordings.db keyed by file name; empty if unreadable."""
    rows: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return rows
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
        want = [c for c in ("ZPATH", "ZDATE", "ZDURATION", "ZENCRYPTEDTITLE", "ZCUSTOMLABEL", "ZUNIQUEID") if c in cols]
        if "ZPATH" not in want:
            return rows
        for r in con.execute(f"SELECT {', '.join(want)} FROM ZCLOUDRECORDING"):
            d = dict(zip(want, r))
            if d.get("ZPATH"):
                rows[Path(d["ZPATH"]).name] = d
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return rows


def scan_voice_memos(root: Optional[Path] = None, cursor: Optional[Cursor] = None) -> ScanResult:
    """Voice Memos recordings. Needs Full Disk Access; see ``check_access``."""
    roots = [Path(root)] if root else default_voice_memos_dirs()
    root = next((r for r in roots if r.exists()), roots[0])
    status = check_access(root)
    if status != "ok":
        res = _diff([], cursor, [])  # never drop known memos just because access lapsed
        if status == "denied":
            res.errors.append(f"{root}: needs Full Disk Access")
        return res
    rows = _voice_memo_rows(root / "CloudRecordings.db")
    cands: list[tuple[Recording, list[Path]]] = []
    errors: list[str] = []
    for audio in sorted(p for p in root.iterdir() if p.suffix.lower() in {".m4a", ".qta", ".caf"}):
        try:
            row = rows.get(audio.name, {})
            title = row.get("ZENCRYPTEDTITLE") or row.get("ZCUSTOMLABEL") or audio.stem
            start = None
            if row.get("ZDATE") is not None:
                start = _CORE_DATA_EPOCH + timedelta(seconds=float(row["ZDATE"]))
            key = row.get("ZUNIQUEID") or str(audio)
            rec = _build(SOURCE_VOICE_MEMOS, key, title, audio, None, start)
            if rec.duration is None and row.get("ZDURATION"):
                rec.duration = float(row["ZDURATION"])
            cands.append((rec, rec.files()))
        except OSError as e:
            errors.append(f"{audio}: {type(e).__name__}: {e}")
    res = _diff(cands, cursor, [root])
    res.errors = errors
    return res


# --------------------------------------------------------------------------- transcription


class EngineUnavailable(RuntimeError):
    """The engine's package or the macOS audio decoder is missing."""


class TranscriptionEngine(Protocol):
    name: str

    def is_installed(self) -> bool: ...
    def model_ready(self) -> bool: ...
    def model_download_size(self) -> int: ...
    def ensure_model(self, progress: Optional[Progress] = None) -> Path: ...
    def transcribe(self, audio: Path, progress: Optional[Progress] = None) -> list[Segment]: ...


@dataclass(frozen=True)
class WhisperModel:
    name: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{self.name}.bin"

    @property
    def filename(self) -> str:
        return f"ggml-{self.name}.bin"


# SHA-256 from Hugging Face's X-Linked-ETag, checked against a real download for base.en.
WHISPER_MODELS = {
    "base.en": WhisperModel("base.en", 147_964_211, "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"),
    "base": WhisperModel("base", 147_951_465, "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
    "small": WhisperModel("small", 487_601_967, "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    "large-v3-turbo-q5_0": WhisperModel("large-v3-turbo-q5_0", 574_041_195,
                                        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"),
}


def default_model_name() -> str:
    return "large-v3-turbo-q5_0" if platform.machine() == "arm64" else "base"


def download_file(url: str, dest: Path, *, expected_size: int = 0, sha256: Optional[str] = None,
                  progress: Optional[Progress] = None, opener=urllib.request.urlopen) -> Path:
    """Stream ``url`` to ``dest`` via a .part file, reporting progress and checking the hash."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "SafeContext"})
    try:
        with opener(req) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0) or expected_size
            done = 0
            if progress:
                progress(0, total)
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if sha256 and h.hexdigest() != sha256:
            raise IOError(f"checksum mismatch for {dest.name}")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def decode_to_wav(src: Path, dest: Path) -> Path:
    """16 kHz mono 16-bit WAV, with macOS's afconvert (ffmpeg as a fallback elsewhere)."""
    if shutil.which("afconvert"):
        cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dest)]
    elif shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    else:
        raise EngineUnavailable("no audio decoder (afconvert or ffmpeg) found")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"could not decode {src.name}: {proc.stderr.strip()[:200]}")
    return dest


class WhisperCppEngine:
    """whisper.cpp through pywhispercpp; Metal on Apple Silicon, CPU on Intel."""

    name = "whisper.cpp"

    def __init__(self, model: Optional[str] = None, models_dir: Optional[Path] = None,
                 language: Optional[str] = None, threads: Optional[int] = None):
        self.model = WHISPER_MODELS[model or default_model_name()]
        self.models_dir = Path(models_dir) if models_dir else default_models_dir()
        self.language = language
        self.threads = threads or max(1, min(8, (os.cpu_count() or 4) - 1))
        self._loaded = None

    @property
    def model_path(self) -> Path:
        return self.models_dir / self.model.filename

    def is_installed(self) -> bool:
        try:
            import pywhispercpp.model  # noqa: F401
        except Exception:
            return False
        return True

    def model_ready(self) -> bool:
        return self.model_path.exists()

    def model_download_size(self) -> int:
        return 0 if self.model_ready() else self.model.size

    def ensure_model(self, progress: Optional[Progress] = None) -> Path:
        if not self.model_ready():
            download_file(self.model.url, self.model_path, expected_size=self.model.size,
                          sha256=self.model.sha256, progress=progress)
        return self.model_path

    def _model(self):
        if self._loaded is None:
            try:
                from pywhispercpp.model import Model
            except Exception as e:
                raise EngineUnavailable(f"pywhispercpp is not installed: {e}") from e
            params = {"print_progress": False, "print_realtime": False, "n_threads": self.threads}
            if self.language:
                params["language"] = self.language
            self._loaded = Model(str(self.ensure_model()), redirect_whispercpp_logs_to=None, **params)
        return self._loaded

    def transcribe(self, audio: Path, progress: Optional[Progress] = None) -> list[Segment]:
        model = self._model()
        with tempfile.TemporaryDirectory(prefix="safecontext-asr-") as tmp:
            wav = decode_to_wav(Path(audio), Path(tmp) / "audio.wav")
            with wave.open(str(wav), "rb") as w:
                total_ms = int(w.getnframes() * 1000 / w.getframerate())

            def on_segment(seg):
                if progress:
                    progress(min(seg.t1 * 10, total_ms), total_ms)

            raw = model.transcribe(str(wav), new_segment_callback=on_segment)
        if progress:
            progress(total_ms, total_ms)
        # pywhispercpp times are in centiseconds.
        return [Segment(s.t0 / 100.0, s.t1 / 100.0, s.text.strip()) for s in raw if s.text.strip()]


def get_engine(model: Optional[str] = None, models_dir: Optional[Path] = None) -> Optional[WhisperCppEngine]:
    """The default engine, or None when its package is not installed."""
    engine = WhisperCppEngine(model, models_dir)
    return engine if engine.is_installed() else None


def transcribe_recording(rec: Recording, engine: TranscriptionEngine,
                         progress: Optional[Progress] = None) -> Recording:
    """A copy of ``rec`` with segments from the engine.

    With per-speaker tracks each one is transcribed alone and labelled with its
    speaker, then merged in time order; otherwise the mixed audio is used.
    """
    if not rec.needs_transcription:
        return rec
    tracks = list(rec.speaker_tracks.items()) or [(None, rec.audio_path)]
    segs: list[Segment] = []
    for i, (speaker, path) in enumerate(tracks):
        def sub(done, total, i=i):
            if progress:
                progress(i * 1000 + (done * 1000 // total if total else 0), len(tracks) * 1000)
        segs += [replace(s, speaker=speaker) for s in engine.transcribe(path, sub)]
    segs.sort(key=lambda s: (s.start if s.start is not None else 0.0))
    duration = rec.duration or (segs[-1].end if segs else None)
    return replace(rec, segments=segs, transcript_format="whisper", duration=duration)


# --------------------------------------------------------------------------- all sources


def scan_all(folders: Iterable[Path] = (), cursor: Optional[Cursor] = None, *,
             zoom: bool = True, voice_memos: bool = True,
             zoom_dir: Optional[Path] = None, voice_memos_dir: Optional[Path] = None,
             participants_resolver: Optional[Callable[[Recording], list[str]]] = None) -> ScanResult:
    """Every enabled source against one cursor.

    ``participants_resolver`` lets the caller fill participants from elsewhere,
    e.g. a calendar event overlapping ``start_time`` (EventKit needs the Calendar
    permission, so that lookup belongs to the app, not this reader).
    """
    cur = cursor or Cursor()
    out = ScanResult([], [], cur)
    steps: list[Callable[[Cursor], ScanResult]] = [lambda c, f=f: scan_folder(f, c) for f in folders]
    if zoom:
        steps.append(lambda c: scan_zoom(zoom_dir, c))
    if voice_memos:
        steps.append(lambda c: scan_voice_memos(voice_memos_dir, c))
    seen: set[str] = set()
    for step in steps:
        res = step(out.cursor)
        for r in res.recordings:
            if r.id in seen:
                continue
            seen.add(r.id)
            if participants_resolver and not r.participants:
                try:
                    r.participants = list(participants_resolver(r))
                except Exception:
                    pass
            out.recordings.append(r)
        out.removed += res.removed
        out.errors += res.errors
        out.cursor = res.cursor
    return out
