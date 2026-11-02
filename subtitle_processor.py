from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

import arabic_reshaper
from bidi.algorithm import get_display

from config import AppConfig

class SubtitleProcessingError(RuntimeError):
    pass

def validate_video_file(video_path: Path) -> None:
    if not video_path.exists():
        raise SubtitleProcessingError(f"Video file does not exist: {video_path}")
    if video_path.stat().st_size <= 0:
        raise SubtitleProcessingError(f"Video file is empty: {video_path}")
    ext = video_path.suffix.lower()
    if ext not in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
        raise SubtitleProcessingError(f"Unsupported video extension: {ext}")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise SubtitleProcessingError(f"FFprobe could not read video metadata: {video_path}")

def validate_srt_file(srt_path: Path) -> str:
    if not srt_path.exists():
        raise SubtitleProcessingError(f"Subtitle file does not exist: {srt_path}")
    if srt_path.stat().st_size <= 0:
        raise SubtitleProcessingError(f"Subtitle file is empty: {srt_path}")
    if srt_path.suffix.lower() != ".srt":
        raise SubtitleProcessingError(f"Invalid subtitle extension: {srt_path.suffix}")

    try:
        raw = srt_path.read_bytes()
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubtitleProcessingError("SRT file must be UTF-8 or UTF-8-BOM.") from exc

    if not re.search(r"\d{2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{1,3}", text):
        raise SubtitleProcessingError("SRT file is malformed or missing valid timestamp lines.")
    return text

def sanitize_ass_text(text: str) -> str:
    text = text.replace("\r", "")
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    text = text.replace("\n", "\\N")
    text = text.replace("|", "\\|")
    return text

def parse_srt_entries(srt_text: str) -> List[Tuple[str, str, str]]:
    entries: List[Tuple[str, str, str]] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        ts = lines[1].strip() if len(lines) > 1 else ""
        match = re.search(r"(\d{2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{1,3})", ts)
        if not match:
            continue
        start = match.group(1)
        end = match.group(2)
        text = "\n".join(lines[2:])
        entries.append((start, end, text))
    return entries

def _srt_time_to_ass_time(value: str) -> str:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) != 3:
        raise SubtitleProcessingError(f"Invalid SRT timestamp: {value}")
    h, m, s = parts
    sec_and_ms = s.split(".")
    if len(sec_and_ms) != 2:
        raise SubtitleProcessingError(f"Invalid SRT timestamp: {value}")
    sec, ms = sec_and_ms
    return f"{h}:{m}:{sec}.{ms.zfill(2)}"

def srt_to_ass(srt_path: Path, ass_path: Path) -> None:
    text = validate_srt_file(srt_path)
    entries = parse_srt_entries(text)
    if not entries:
        raise SubtitleProcessingError("No valid subtitle entries found in the SRT file.")

    ass_lines = [
        "[Script Info]",
        "Title: TeliCinema HardSub",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.601",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: SubtitleStyle,Noto Naskh Arabic,28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,30,30,28,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for start, end, raw_text in entries:
        clean = raw_text
        clean = re.sub(r"<.*?>", "", clean)
        clean = clean.replace("\r", "").strip()
        if not clean:
            continue
        reshaped = arabic_reshaper.reshape(clean)
        rtl_text = get_display(reshaped)
        safe_text = sanitize_ass_text(rtl_text)
        start_ass = _srt_time_to_ass_time(start)
        end_ass = _srt_time_to_ass_time(end)
        ass_lines.append(
            f"Dialogue: 0,{start_ass},{end_ass},SubtitleStyle,,0,0,0,,{{\\an5\\pos(960,900)\\q2\\p1\\c&HFFFFFF&\\3c&H000000&\\b1\\fs28}}{safe_text}"
        )

    ass_path.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")

async def _run_ffmpeg(cmd: List[str], progress_callback=None) -> None:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if process.stdout is None or process.stderr is None:
        raise SubtitleProcessingError("FFmpeg stdout/stderr could not be captured.")

    while True:
        line = await process.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        if progress_callback:
            progress_callback(text)

    await process.wait()
    if process.returncode != 0:
        raise SubtitleProcessingError("FFmpeg failed to render subtitles. See logs for details.")

async def process_video_with_hardcoded_subtitles(
    input_video: Path,
    subtitle_srt: Path,
    output_path: Path,
    config: AppConfig,
    progress_callback=None,
    cancel_event: asyncio.Event | None = None,
) -> Path:
    validate_video_file(input_video)
    validate_srt_file(subtitle_srt)

    tmp_dir = output_path.parent
    ass_path = tmp_dir / "embedded_subtitles.ass"
    srt_to_ass(subtitle_srt, ass_path)

    if output_path.exists():
        output_path.unlink()

    ffmpeg_font_dir = "/usr/share/fonts/truetype/dejavu"
    filter_arg = (
        f"subtitles='{ass_path.as_posix()}':fontsdir={ffmpeg_font_dir}:force_style='FontName=Noto Naskh Arabic,Fontsize=28,PrimaryColour=&HFFFFFFFF,OutlineColour=&H80000000,BackColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=30'"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_video),
        "-vf", filter_arg,
        "-c:v", "libx264",
        "-preset", config.FFMPEG_PRESET,
        "-crf", str(config.FFMPEG_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-map", "0:v:0",
        "-map", "0:a?",
        "-sn",
        "-dn",
        str(output_path),
    ]

    try:
        async def check_cancel():
            while cancel_event is not None and not cancel_event.is_set():
                await asyncio.sleep(0.25)
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("Processing cancelled by user.")

        await asyncio.gather(_run_ffmpeg(cmd, progress_callback), check_cancel())
    except asyncio.CancelledError:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        if ass_path.exists():
            ass_path.unlink(missing_ok=True)
        raise SubtitleProcessingError(f"FFmpeg rendering failed: {exc}") from exc

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise SubtitleProcessingError("Output video was not created or is empty.")

    if ass_path.exists():
        ass_path.unlink(missing_ok=True)

    return output_path