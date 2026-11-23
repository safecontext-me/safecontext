"""The call-recordings reader. Only fixtures and generated audio: never the user's real recordings."""
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import time
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from safecontext.readers import recordings as rec
from safecontext.readers.recordings import Cursor, Segment

FIX = Path(__file__).parent / "fixtures" / "recordings"


# --------------------------------------------------------------------------- helpers


def make_wav(path: Path, seconds: float = 1.5, rate: int = 16000) -> Path:
    """A tiny generated clip: a quiet 440 Hz tone."""
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def make_m4a(path: Path, duration: float = 12.5, created: datetime | None = None,
             tsrp: dict | None = None) -> Path:
    """A minimal MP4 container: ftyp, mdat, moov(mvhd, trak(udta(tsrp)))."""
    created = created or datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    secs = int((created - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())
    mvhd = box(b"mvhd", bytes(4) + struct.pack(">IIII", secs, secs, 1000, int(duration * 1000)) + bytes(80))
    trak_children = b""
    if tsrp is not None:
        trak_children = box(b"udta", box(b"tsrp", json.dumps(tsrp).encode()))
    data = box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"mdat", bytes(4096)) \
        + box(b"moov", mvhd + box(b"trak", trak_children))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_docx(path: Path, paragraphs: list[str], title: str | None = None) -> Path:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    core = ('<?xml version="1.0"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
            + (f"<dc:title>{title}</dc:title>" if title else "")
            + '<dcterms:created>2026-02-10T09:30:00Z</dcterms:created></cp:coreProperties>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("docProps/core.xml", core)
    return path


class FakeEngine:
    name = "fake"

    def __init__(self, segments=None):
        self.calls = []
        self.segments = segments or [Segment(0.0, 1.0, "hello there")]

    def is_installed(self):
        return True

    def model_ready(self):
        return True

    def model_download_size(self):
        return 0

    def ensure_model(self, progress=None):
        return Path("/nonexistent")

    def transcribe(self, audio, progress=None):
        self.calls.append(Path(audio))
        if progress:
            progress(1, 1)
        return list(self.segments)


# --------------------------------------------------------------------------- transcript formats


def test_zoom_cloud_vtt_names_from_cue_text():
    tf = rec.read_transcript(FIX / "zoom_cloud.transcript.vtt")
    assert tf.format == "vtt"
    assert [(s.start, s.end, s.speaker) for s in tf.segments] == [(1.0, 4.5, "Dana Lee"), (4.6, 8.0, "Sam Ortiz")]
    assert tf.segments[0].text == "Thanks for joining, everyone."
    assert tf.participants == ["Dana Lee", "Sam Ortiz"]


def test_teams_vtt_voice_tags_merge_split_cues_and_unescape():
    segs = rec.read_transcript(FIX / "teams.vtt").segments
    assert [(s.speaker, s.text) for s in segs] == [
        ("Priya Shah", "We should ship on Tuesday, not Monday."),
        ("Sam Ortiz", "Agreed & noted."),
    ]
    assert segs[0].start == 0.5 and segs[0].end == 3.9


def test_srt_and_generic_speaker_labels_are_not_participants():
    tf = rec.read_transcript(FIX / "otter.srt")
    assert [s.speaker for s in tf.segments] == ["Speaker 1", "Speaker 2"]
    assert tf.segments[1].start == 3.4
    assert tf.participants == []


def test_otter_text_export():
    tf = rec.read_transcript(FIX / "otter_export.txt")
    assert [(s.speaker, s.start) for s in tf.segments] == [("Dana Lee", 0), ("Sam Ortiz", 7), ("Dana Lee", 3725)]
    assert tf.segments[1].text == "We are on track. The docs are nearly finished."
    assert tf.segments[0].end == 7  # filled from the next segment
    assert all("otter.ai" not in s.text for s in tf.segments)


def test_google_meet_transcript_title_attendees_and_times():
    tf = rec.read_transcript(FIX / "meet_transcript.txt")
    assert tf.title == "Roadmap sync"
    assert tf.participants == ["Dana Lee", "Sam Ortiz", "Priya Shah"]
    assert [(s.speaker, s.start) for s in tf.segments] == [
        ("Dana Lee", 0), ("Sam Ortiz", None), ("Priya Shah", 90)]
    assert not any("computer generated" in s.text for s in tf.segments)


def test_granola_markdown():
    tf = rec.read_transcript(FIX / "granola.md")
    assert tf.title == "Intro call with Acme"
    assert [s.speaker for s in tf.segments] == ["Me", "Them", "Me"]
    assert tf.looks_like_transcript
    assert tf.participants == []


def test_plain_notes_are_not_transcripts():
    assert not rec.read_transcript(FIX / "shopping_notes.md").looks_like_transcript


def test_zoom_saved_captions_relative_to_first_line():
    segs = rec.read_transcript(FIX / "closed_caption.txt").segments
    assert [(s.speaker, s.start) for s in segs] == [("Dana Lee", 0), ("Sam Ortiz", 4)]


def test_teams_docx(tmp_path):
    p = make_docx(tmp_path / "Weekly sync.docx",
                  ["Weekly sync", "Dana Lee   0:03", "Morning all.", "Sam Ortiz   0:09", "Hi Dana."],
                  title="Weekly sync")
    tf = rec.read_transcript(p)
    assert tf.format == "docx"
    assert [(s.speaker, s.start, s.text) for s in tf.segments] == [
        (None, None, "Weekly sync"), ("Dana Lee", 3, "Morning all."), ("Sam Ortiz", 9, "Hi Dana.")]
    assert tf.start_time == datetime(2026, 2, 10, 9, 30, tzinfo=timezone.utc)


def test_bracketed_timestamp_lines():
    out = rec.parse_text_transcript("[00:00:05] Dana Lee: Hi\n[00:00:09] Sam: Hello\n")
    assert [(s.speaker, s.start, s.text) for s in out.segments] == [("Dana Lee", 5, "Hi"), ("Sam", 9, "Hello")]


# --------------------------------------------------------------------------- audio metadata


def test_audio_info_wav(tmp_path):
    info = rec.audio_info(make_wav(tmp_path / "a.wav", seconds=1.5))
    assert info.duration == pytest.approx(1.5)


def test_audio_info_mp4_duration_and_created(tmp_path):
    created = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    info = rec.audio_info(make_m4a(tmp_path / "a.m4a", duration=12.5, created=created))
    assert info.duration == pytest.approx(12.5)
    assert info.created == created
    assert info.embedded_transcript is None


def test_voice_memos_embedded_transcript(tmp_path):
    tsrp = {"attributedString": {
        "runs": ["Remember ", 0, "to ", 1, "call ", 2, "Sam.", 3, " Then ", 4, "lunch.", 5],
        "attributeTable": [{"timeRange": [0.0, 0.4]}, {"timeRange": [0.4, 0.6]}, {"timeRange": [0.6, 0.9]},
                           {"timeRange": [0.9, 1.3]}, {"timeRange": [4.0, 4.3]}, {"timeRange": [4.3, 4.8]}]}}
    info = rec.audio_info(make_m4a(tmp_path / "memo.m4a", tsrp=tsrp))
    assert [(s.start, s.end, s.text) for s in info.embedded_transcript] == [
        (0.0, 1.3, "Remember to call Sam."), (4.0, 4.8, "Then lunch.")]


def test_audio_info_garbage_is_empty(tmp_path):
    p = tmp_path / "broken.m4a"
    p.write_bytes(b"\x00\x00\x00\x02junk")
    assert rec.audio_info(p) == rec.AudioInfo()


# --------------------------------------------------------------------------- watched folder + cursor


def test_scan_folder_pairs_audio_with_transcript_and_skips_notes(tmp_path):
    folder = tmp_path / "calls"
    make_wav(folder / "2026-05-01 14.30 Budget call.wav")
    shutil.copy(FIX / "teams.vtt", folder / "2026-05-01 14.30 Budget call.vtt")
    make_wav(folder / "sub" / "untranscribed.wav")
    shutil.copy(FIX / "granola.md", folder / "sub" / "granola.md")
    shutil.copy(FIX / "shopping_notes.md", folder / "notes.md")
    (folder / "photo.jpg").write_bytes(b"x")

    res = rec.scan_folder(folder)
    by_title = {r.title: r for r in res.recordings}
    assert set(by_title) == {"2026-05-01 14.30 Budget call", "untranscribed", "Intro call with Acme"}

    budget = by_title["2026-05-01 14.30 Budget call"]
    assert budget.audio_path.suffix == ".wav" and budget.transcript_path.suffix == ".vtt"
    assert budget.path == budget.audio_path
    assert budget.start_time == datetime(2026, 5, 1, 14, 30).astimezone()
    assert budget.duration == pytest.approx(1.5)
    assert budget.participants == ["Priya Shah", "Sam Ortiz"]
    assert not budget.needs_transcription

    assert by_title["untranscribed"].needs_transcription
    granola = by_title["Intro call with Acme"]
    assert granola.audio_path is None and granola.path.name == "granola.md"
    assert len({r.id for r in res.recordings}) == 3


def test_cursor_only_new_or_changed_and_removed(tmp_path):
    folder = tmp_path / "calls"
    a = make_wav(folder / "a.wav")
    shutil.copy(FIX / "otter.srt", folder / "b.srt")

    first = rec.scan_folder(folder)
    assert len(first.recordings) == 2
    cur = Cursor.from_json(first.cursor.to_json())  # round-trips

    again = rec.scan_folder(folder, cur)
    assert again.recordings == [] and again.removed == []

    # Touched but identical: not re-emitted.
    os.utime(a, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    touched = rec.scan_folder(folder, again.cursor)
    assert touched.recordings == []

    # A transcript arrives for a.wav: same recording, now changed.
    shutil.copy(FIX / "zoom_cloud.transcript.vtt", folder / "a.vtt")
    paired = rec.scan_folder(folder, touched.cursor)
    assert [r.audio_path.name for r in paired.recordings] == ["a.wav"]
    assert paired.recordings[0].segments

    (folder / "b.srt").unlink()
    gone = rec.scan_folder(folder, paired.cursor)
    b_id = next(r.id for r in first.recordings if r.path.name == "b.srt")
    assert gone.recordings == [] and gone.removed == [b_id]
    assert str(folder / "b.srt") not in gone.cursor.files


def test_cursor_from_empty():
    assert Cursor.from_json(None).files == {}
    assert Cursor.from_json("") .files == {}


def test_quick_hash_reads_ends_only_for_big_files(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"a" * (3 << 20))
    h1 = rec._quick_hash(p)
    with open(p, "r+b") as f:
        f.seek(3 << 19)
        f.write(b"b")  # the middle is not hashed
    assert rec._quick_hash(p) == h1
    with open(p, "r+b") as f:
        f.seek(-1, 2)
        f.write(b"b")
    assert rec._quick_hash(p) != h1


# --------------------------------------------------------------------------- Zoom


def make_zoom_meeting(root: Path) -> Path:
    m = root / "2026-04-02 10.15.00 Design review 81234567890"
    make_m4a(m / "audio1234567890.m4a", duration=300)
    (m / "video1234567890.mp4").write_bytes(b"\x00" * 16)
    make_m4a(m / "Audio Record" / "audioDanaLee11234567890.m4a", duration=300)
    make_m4a(m / "Audio Record" / "audioSamOrtiz21234567890.m4a", duration=300)
    (m / "meeting_saved_chat.txt").write_text(
        "10:16:03 From Priya Shah to Everyone:\n\tlink to the doc\n10:17:00 From Dana Lee to Everyone:\n\tthanks\n")
    return m


def test_scan_zoom_meeting_folder(tmp_path):
    root = tmp_path / "Zoom"
    make_zoom_meeting(root)
    (root / "empty meeting").mkdir()
    res = rec.scan_zoom(root)
    [r] = res.recordings
    assert r.source == rec.SOURCE_ZOOM
    assert r.title == "Design review"
    assert r.start_time == datetime(2026, 4, 2, 10, 15).astimezone()
    assert r.audio_path.name == "audio1234567890.m4a"
    assert r.duration == pytest.approx(300)
    assert set(r.speaker_tracks) == {"DanaLee", "SamOrtiz"}
    assert r.participants == ["DanaLee", "SamOrtiz", "Priya Shah", "Dana Lee"]
    assert r.needs_transcription
    assert rec.scan_zoom(root, res.cursor).recordings == []


def test_scan_zoom_with_dropped_cloud_transcript(tmp_path):
    root = tmp_path / "Zoom"
    m = make_zoom_meeting(root)
    shutil.copy(FIX / "zoom_cloud.transcript.vtt", m / "GMT20260402-101500_Recording.transcript.vtt")
    [r] = rec.scan_zoom(root).recordings
    assert r.transcript_format == "vtt" and not r.needs_transcription


def test_scan_zoom_missing_dir(tmp_path):
    res = rec.scan_zoom(tmp_path / "nope")
    assert res.recordings == [] and res.errors == []


def test_default_zoom_dir_uses_home():
    # conftest points HOME at a temporary folder, so this never touches real recordings.
    assert rec.default_zoom_dir() == Path.home() / "Documents" / "Zoom"
    assert rec.scan_zoom().recordings == []


# --------------------------------------------------------------------------- Voice Memos


def test_scan_voice_memos_with_database(tmp_path):
    root = tmp_path / "Recordings"
    make_m4a(root / "20260301 120000-ABCD.m4a", duration=42)
    make_m4a(root / "untitled.m4a", duration=5)
    con = sqlite3.connect(root / "CloudRecordings.db")
    con.execute("CREATE TABLE ZCLOUDRECORDING (Z_PK INTEGER, ZDATE REAL, ZDURATION REAL, "
                "ZENCRYPTEDTITLE TEXT, ZCUSTOMLABEL TEXT, ZPATH TEXT, ZUNIQUEID TEXT)")
    zdate = (datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc) - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()
    con.execute("INSERT INTO ZCLOUDRECORDING VALUES (1, ?, 42.0, 'Call with landlord', NULL, ?, 'UUID-1')",
                (zdate, "20260301 120000-ABCD.m4a"))
    con.commit()
    con.close()

    res = rec.scan_voice_memos(root)
    by = {r.audio_path.name: r for r in res.recordings}
    memo = by["20260301 120000-ABCD.m4a"]
    assert memo.title == "Call with landlord"
    assert memo.start_time == datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert memo.id == rec._recording_id(rec.SOURCE_VOICE_MEMOS, "UUID-1")
    assert by["untitled.m4a"].title == "untitled"


def test_voice_memos_missing_and_denied(tmp_path, monkeypatch):
    assert rec.scan_voice_memos(tmp_path / "none").recordings == []
    root = tmp_path / "locked"
    root.mkdir()
    real_listdir = os.listdir

    def deny(p):
        if Path(p) == root:
            raise PermissionError(1, "Operation not permitted")
        return real_listdir(p)

    monkeypatch.setattr(rec.os, "listdir", deny)
    assert rec.check_access(root) == "denied"
    old = Cursor({str(root / "x.m4a"): rec.FileState(1, 1, "d", "id1")})
    res = rec.scan_voice_memos(root, old)
    assert res.errors == [f"{root}: needs Full Disk Access"]
    assert res.removed == [] and res.cursor.files == old.files


# --------------------------------------------------------------------------- transcription


def test_transcribe_recording_with_mock_engine(tmp_path):
    folder = tmp_path / "calls"
    make_wav(folder / "call.wav")
    [r] = rec.scan_folder(folder).recordings
    engine = FakeEngine([Segment(0.0, 0.7, "hello"), Segment(0.7, 1.4, "bye")])
    seen = []
    done = rec.transcribe_recording(r, engine, progress=lambda d, t: seen.append((d, t)))
    assert engine.calls == [folder / "call.wav"]
    assert done.transcript_format == "whisper"
    assert done.text == "hello\nbye"
    assert not done.needs_transcription and r.needs_transcription  # original untouched
    assert seen[-1] == (1000, 1000)
    assert rec.transcribe_recording(done, engine) is done


def test_transcribe_zoom_tracks_are_labelled_and_merged(tmp_path):
    root = tmp_path / "Zoom"
    make_zoom_meeting(root)
    [r] = rec.scan_zoom(root).recordings

    class PerTrack(FakeEngine):
        def transcribe(self, audio, progress=None):
            self.calls.append(Path(audio))
            if "Dana" in Path(audio).name:
                return [Segment(0.0, 2.0, "hi"), Segment(5.0, 6.0, "ok")]
            return [Segment(2.5, 4.0, "hello")]

    engine = PerTrack()
    done = rec.transcribe_recording(r, engine)
    assert len(engine.calls) == 2 and r.audio_path not in engine.calls
    assert [(s.speaker, s.text) for s in done.segments] == [
        ("DanaLee", "hi"), ("SamOrtiz", "hello"), ("DanaLee", "ok")]


def test_engine_reports_missing_package(monkeypatch, tmp_path):
    import builtins
    real_import = builtins.__import__

    def no_whisper(name, *a, **k):
        if name.startswith("pywhispercpp"):
            raise ImportError("no pywhispercpp")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_whisper)
    assert rec.get_engine(models_dir=tmp_path) is None
    engine = rec.WhisperCppEngine("base.en", models_dir=tmp_path)
    assert not engine.is_installed()
    (tmp_path / "ggml-base.en.bin").write_bytes(b"x")
    with pytest.raises(rec.EngineUnavailable):
        engine.transcribe(tmp_path / "a.wav")


def test_engine_model_paths_and_defaults(tmp_path, monkeypatch):
    e = rec.WhisperCppEngine("small", models_dir=tmp_path)
    assert e.model_path == tmp_path / "ggml-small.bin"
    assert not e.model_ready() and e.model_download_size() == 487_601_967
    assert rec.default_models_dir() == Path.home() / "Library" / "Application Support" / "SafeContext" / "models" / "whisper"
    monkeypatch.setattr(rec.platform, "machine", lambda: "x86_64")
    assert rec.default_model_name() == "base"
    monkeypatch.setattr(rec.platform, "machine", lambda: "arm64")
    assert rec.default_model_name() == "large-v3-turbo-q5_0"


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_download_file_progress_and_checksum(tmp_path):
    import hashlib
    data = os.urandom(3 * (1 << 20) + 5)
    seen = []
    out = rec.download_file("https://example.invalid/m.bin", tmp_path / "m" / "model.bin",
                            sha256=hashlib.sha256(data).hexdigest(),
                            progress=lambda d, t: seen.append((d, t)),
                            opener=lambda req: FakeResponse(data))
    assert out.read_bytes() == data
    assert seen[0] == (0, len(data)) and seen[-1] == (len(data), len(data)) and len(seen) == 5

    with pytest.raises(IOError):
        rec.download_file("https://example.invalid/m.bin", tmp_path / "bad.bin", sha256="0" * 64,
                          opener=lambda req: FakeResponse(data))
    assert not (tmp_path / "bad.bin").exists()
    assert not (tmp_path / "bad.bin.part").exists()


@pytest.mark.skipif(not shutil.which("afconvert"), reason="needs macOS afconvert")
def test_decode_to_wav_with_afconvert(tmp_path):
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=44100)
    out = rec.decode_to_wav(src, tmp_path / "out.wav")
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)


def test_scan_all_combines_sources_and_resolves_participants(tmp_path):
    folder = tmp_path / "calls"
    make_wav(folder / "call.wav")
    zoom = tmp_path / "Zoom"
    make_zoom_meeting(zoom)
    res = rec.scan_all([folder], zoom_dir=zoom, voice_memos_dir=tmp_path / "no-memos",
                       participants_resolver=lambda r: ["From Calendar"])
    by_source = {r.source: r for r in res.recordings}
    assert set(by_source) == {"folder", "zoom"}
    assert by_source["folder"].participants == ["From Calendar"]
    assert by_source["zoom"].participants[0] == "DanaLee"
    again = rec.scan_all([folder], res.cursor, zoom_dir=zoom, voice_memos_dir=tmp_path / "no-memos")
    assert again.recordings == [] and again.removed == []


# --------------------------------------------------------------------------- real engine (opt-in)


@pytest.mark.skipif(not os.environ.get("SAFECONTEXT_WHISPER_MODELS"),
                    reason="set SAFECONTEXT_WHISPER_MODELS to a folder holding ggml-base.en.bin")
def test_real_whisper_transcription(tmp_path):
    if not shutil.which("say") or not shutil.which("afconvert"):
        pytest.skip("needs macOS say and afconvert")
    engine = rec.WhisperCppEngine("base.en", models_dir=Path(os.environ["SAFECONTEXT_WHISPER_MODELS"]))
    if not engine.is_installed() or not engine.model_ready():
        pytest.skip("pywhispercpp or the model is not installed")
    aiff = tmp_path / "clip.aiff"
    subprocess.run(["say", "-o", str(aiff), "Let's meet on Tuesday at noon."], check=True)
    folder = tmp_path / "calls"
    folder.mkdir()
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(folder / "clip.m4a")], check=True)
    [r] = rec.scan_folder(folder).recordings
    t = time.time()
    done = rec.transcribe_recording(r, engine)
    print(f"transcribed {r.duration:.1f}s of audio in {time.time() - t:.2f}s")
    assert "tuesday" in done.text.lower()
