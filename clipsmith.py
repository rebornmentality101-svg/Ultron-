#!/usr/bin/env python3
"""
clipsmith - cut long YouTube videos into vertical Shorts, and optionally upload them.

    python3 clipsmith.py "https://youtu.be/VIDEO_ID" --auto 3 --duration 45
    python3 clipsmith.py "https://youtu.be/VIDEO_ID" --clips 1:30-2:05,10:00-10:40 --upload

Needs ffmpeg + ffprobe on PATH. Uploading additionally needs the packages in
requirements.txt and a Google OAuth client (see README.md).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

TOKEN_FILE = ".clipsmith-token.json"
CLIENT_FILE = "client_secret.json"
SHORTS_MAX_SECONDS = 180  # YouTube treats vertical videos up to 3 minutes as Shorts
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CATEGORY_PEOPLE_BLOGS = "22"


# --------------------------------------------------------------------------- #
# shell helpers
# --------------------------------------------------------------------------- #

def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def require_media_tools() -> None:
    missing = [c for c in ("ffmpeg", "ffprobe") if not have(c)]
    if missing:
        fail(
            f"missing {', '.join(missing)} on PATH.\n"
            "  macOS:   brew install ffmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg   (then reopen your terminal)"
        )


# --------------------------------------------------------------------------- #
# media inspection
# --------------------------------------------------------------------------- #

def probe(path: Path) -> dict:
    """Return {duration, width, height} for a video file."""
    out = run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ])
    if out.returncode != 0:
        fail(f"ffprobe could not read {path.name}: {out.stderr.strip()}")
    data = json.loads(out.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


def has_audio(path: Path) -> bool:
    out = run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
    ])
    return bool(out.stdout.strip())


def hhmmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #

def download(url: str, outdir: Path, max_height: int) -> tuple[Path, str]:
    """Fetch the source video into outdir. Returns (path, title)."""
    fmt = (
        f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]"
        f"/b[height<={max_height}]/bv*+ba/b"
    )
    template = str(outdir / "source.%(ext)s")
    title = ""

    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError:
        exe = shutil.which("yt-dlp")
        if not exe:
            fail("yt-dlp is not installed. Run:  pip install -r requirements.txt")
        print("downloading source video ...", flush=True)
        proc = subprocess.run([
            exe, "-f", fmt, "--merge-output-format", "mp4",
            "-o", template, url,
        ])
        if proc.returncode != 0:
            fail("yt-dlp could not download that URL.")
    else:
        print("downloading source video ...", flush=True)
        opts = {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": template,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "restrictfilenames": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            fail(f"yt-dlp could not download that URL: {exc}")
        title = str(info.get("title") or "")

    found = sorted(outdir.glob("source.*"), key=lambda p: p.stat().st_size)
    if not found:
        fail("download finished but no source file was produced.")
    return found[-1], title


# --------------------------------------------------------------------------- #
# highlight detection (audio energy)
# --------------------------------------------------------------------------- #

_WINDOW_SECONDS = 0.5


def loudness_profile(src: Path) -> list[float]:
    """Per-window RMS loudness in dB, one entry every _WINDOW_SECONDS."""
    samples = int(8000 * _WINDOW_SECONDS)  # 0.5 s at 8 kHz
    out = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
        "-map", "0:a:0",
        "-af",
        f"aresample=8000,asetnsamples=n={samples}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ])
    # ametadata's `print` writes to stderr, which is why we parse that stream.
    text = out.stderr
    values: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if "RMS_level=" not in line:
            continue
        raw = line.split("RMS_level=", 1)[1]
        try:
            values.append(float(raw))
        except ValueError:
            values.append(-90.0)  # -inf (digital silence)
    if not values:
        fail("could not analyse the audio track (does this video have audio?).")
    return values


def _moving_average(values: list[float], radius: int) -> list[float]:
    if radius <= 0 or len(values) < 2:
        return list(values)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def pick_highlights(
    profile: list[float],
    clip_seconds: float,
    count: int,
    total_seconds: float,
) -> list[tuple[float, float]]:
    """Choose `count` non-overlapping loud windows - a decent proxy for the good bits."""
    frame_count = int(round(clip_seconds / _WINDOW_SECONDS))
    total = len(profile)
    if frame_count >= total:
        fail(
            f"the video is only {hhmmss(total_seconds)} long - shorter than one "
            f"{clip_seconds:.0f}s clip. Try --duration {max(5, total_seconds / 2):.0f}."
        )

    smooth = _moving_average(profile, radius=2)

    # Stay clear of intros/outros, where long videos are usually quiet anyway.
    margin = int(min(8.0, max(0.0, total_seconds * 0.01)) / _WINDOW_SECONDS)
    first = margin
    last = total - margin - frame_count
    if last <= first:
        first, last = 0, total - frame_count

    # Rolling sums -> mean loudness of every candidate window.
    prefix = [0.0]
    for v in smooth:
        prefix.append(prefix[-1] + v)

    def score(start: int) -> float:
        return (prefix[start + frame_count] - prefix[start]) / frame_count

    candidates = [(score(i), i) for i in range(first, min(last, total - frame_count) + 1)]
    candidates.sort(reverse=True)

    chosen: list[tuple[float, float]] = []
    blocked = [False] * total
    for _, start in candidates:
        if len(chosen) >= count:
            break
        if any(blocked[start:start + frame_count]):
            continue
        chosen.append((start * _WINDOW_SECONDS, (start + frame_count) * _WINDOW_SECONDS))
        lo, hi = max(0, start - frame_count // 3), min(total, start + frame_count + frame_count // 3)
        for i in range(lo, hi):
            blocked[i] = True

    if not chosen:
        fail("could not find any usable highlights - pass --clips with explicit timestamps.")
    return sorted(chosen)


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

def parse_time(token: str) -> float:
    token = token.strip()
    if not token:
        raise ValueError(token)
    parts = token.split(":")
    if len(parts) > 3:
        raise ValueError(token)
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def parse_clips(spec: str) -> list[tuple[float, float]]:
    clips = []
    for chunk in re.split(r"[,\n;]+", spec):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            fail(f"bad --clips entry '{chunk}', expected START-END e.g. 1:30-2:05")
        start_s, end_s = chunk.split("-", 1)
        start, end = parse_time(start_s), parse_time(end_s)
        if end <= start:
            fail(f"bad --clips entry '{chunk}': end must come after start")
        clips.append((start, end))
    if not clips:
        fail("--clips needs at least one START-END range")
    return clips


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def render(
    src: Path,
    start: float,
    end: float,
    dest: Path,
    *,
    vertical: bool,
    loudness_norm: bool,
    audio: bool,
) -> None:
    duration = end - start
    filters = []
    if vertical:
        # Blurred, zoomed copy fills the 9:16 frame; the real video sits centred.
        filters.append(
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=30[bgb];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
        )
    else:
        filters.append("[0:v]format=yuv420p[v]")
    if audio and loudness_norm:
        filters.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
    ]
    if audio:
        cmd += ["-map", "[a]" if loudness_norm else "0:a:0"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-progress", "pipe:1", "-loglevel", "error",
        str(dest),
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_ms="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            pct = min(100, int(done / duration * 100)) if duration else 0
            print(f"\r  encoding {pct:3d}%", end="", flush=True)
    proc.wait()
    print("\r" + " " * 20 + "\r", end="")
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "").strip().splitlines()
        fail(f"ffmpeg failed on {dest.name}: {err[-1] if err else 'unknown error'}")


# --------------------------------------------------------------------------- #
# YouTube upload
# --------------------------------------------------------------------------- #

def check_upload_ready(client_file: Path) -> None:
    """Fail fast before spending time on rendering."""
    try:
        import googleapiclient  # noqa: F401,PLC0415
        import google_auth_oauthlib  # noqa: F401,PLC0415
    except ImportError:
        fail("upload support is not installed. Run:  pip install -r requirements.txt")
    if not client_file.exists() and not Path(TOKEN_FILE).exists():
        fail(
            f"{client_file} not found. Create an OAuth 'Desktop app' client in "
            "Google Cloud Console and save the JSON here (see README.md)."
        )


def youtube_client(client_file: Path):
    try:
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
        from googleapiclient.discovery import build  # noqa: PLC0415
    except ImportError:
        fail("upload support is not installed. Run:  pip install -r requirements.txt")

    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not client_file.exists():
            fail(
                f"{client_file} not found. Create an OAuth 'Desktop app' client in "
                "Google Cloud Console and save the JSON here (see README.md)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        Path(TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
        print(f"saved credentials to {TOKEN_FILE}")
    return build("youtube", "v3", credentials=creds)


def channel_name(service) -> str:
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    return items[0]["snippet"]["title"] if items else "(unknown channel)"


def upload(service, path: Path, *, title: str, description: str, privacy: str) -> str:
    from googleapiclient.http import MediaFileUpload  # noqa: PLC0415

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": CATEGORY_PEOPLE_BLOGS,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(
        part="snippet,status", body=body, media_body=media,
        notifySubscribers=True,
    )
    print(f"  uploading {path.name} ({privacy}) ...", flush=True)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"\r  uploading {int(status.progress() * 100):3d}%", end="", flush=True)
    print("\r" + " " * 20 + "\r", end="")
    return f"https://youtu.be/{response['id']}"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="clipsmith",
        description="Cut a long YouTube video into vertical Shorts, optionally uploading them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clipsmith.py URL --auto 3 --duration 40\n"
            "  clipsmith.py URL --clips 1:30-2:05,10:00-10:40 --list\n"
            "  clipsmith.py URL --clips 1:30-2:05 --upload --privacy public\n"
        ),
    )
    ap.add_argument("url", nargs="?", help="YouTube URL (also accepts a local file path)")
    ap.add_argument("--clips", help="explicit ranges: '1:30-2:05,10:00-10:40'")
    ap.add_argument("--auto", type=int, metavar="N", help="auto-pick N loud highlights")
    ap.add_argument("--duration", type=float, default=45.0,
                    help="target clip length in seconds for --auto (default 45)")
    ap.add_argument("--out", default="shorts", help="output folder (default: shorts)")
    ap.add_argument("--max-height", type=int, default=1080,
                    help="max source resolution to download (default 1080)")
    ap.add_argument("--original-aspect", action="store_true",
                    help="keep the source aspect ratio instead of 9:16")
    ap.add_argument("--no-normalize", action="store_true",
                    help="skip loudness normalisation")
    ap.add_argument("--list", action="store_true",
                    help="print the chosen segments and stop, without rendering")
    ap.add_argument("--upload", action="store_true", help="upload the rendered clips")
    ap.add_argument("--privacy", default="private",
                    choices=["private", "unlisted", "public"],
                    help="upload privacy (default: private)")
    ap.add_argument("--title", help="title prefix for uploads (default: source video title)")
    ap.add_argument("--whoami", action="store_true",
                    help="show which YouTube channel the saved credentials belong to")
    args = ap.parse_args()

    if args.whoami:
        service = youtube_client(Path(CLIENT_FILE))
        print(f"authenticated as: {channel_name(service)}")
        return

    if not args.url:
        ap.error("give a YouTube URL (or use --whoami)")
    if not args.auto and not args.clips:
        ap.error("pick segments with --auto N or --clips START-END[,START-END]")

    require_media_tools()
    source_arg = Path(args.url)
    is_local = source_arg.exists() and source_arg.is_file()

    outdir = Path(args.out)
    workdir = outdir / ".source"
    workdir.mkdir(parents=True, exist_ok=True)

    if is_local:
        src, source_title = source_arg, source_arg.stem
    else:
        src, source_title = download(args.url, workdir, args.max_height)
    source_title = args.title or source_title or "New video"

    info = probe(src)
    if info["duration"] <= 0:
        fail("could not read the duration of the source video.")
    print(f"source: {hhmmss(info['duration'])}  {info['width']}x{info['height']}")
    audio = has_audio(src)
    if not audio:
        print("note: no audio track, so --auto falls back to the first segment")

    if args.clips:
        segments = parse_clips(args.clips)
    elif audio:
        print("analysing audio for the loudest moments ...", flush=True)
        profile = loudness_profile(src)
        segments = pick_highlights(profile, args.duration, args.auto, info["duration"])
    else:
        args.auto = args.auto or 1
        segments = [(0.0, min(args.duration, info["duration"])) for _ in range(args.auto)]

    clipped = [
        (max(0.0, s), min(e, info["duration"]))
        for s, e in segments
        if min(e, info["duration"]) - max(0.0, s) > 1
    ]
    if not clipped:
        fail("no usable segments - the timestamps fall outside the video.")

    print(f"\n{len(clipped)} clip(s):")
    for i, (s, e) in enumerate(clipped, 1):
        length = e - s
        flag = ""
        if length > SHORTS_MAX_SECONDS:
            flag = "  (over 3 min - may not count as a Short)"
        print(f"  {i}. {hhmmss(s)} - {hhmmss(e)}  ({length:.0f}s){flag}")

    if args.list:
        return

    if args.upload:
        check_upload_ready(Path(CLIENT_FILE))

    rendered: list[Path] = []
    for i, (start, end) in enumerate(clipped, 1):
        dest = outdir / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', source_title)[:60].strip('-') or 'short'}_{i:02d}.mp4"
        print(f"rendering {dest.name} ...")
        render(
            src, start, end, dest,
            vertical=not args.original_aspect,
            loudness_norm=not args.no_normalize,
            audio=audio,
        )
        rendered.append(dest)
 
