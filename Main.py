import subprocess
import sys
import os

CLIP_SECONDS = 45

def get_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ])
    return float(out.strip())

def make_clips(path):
    os.makedirs("clips", exist_ok=True)
    total = get_duration(path)
    start = 0
    n = 1
    while start < total:
        out = f"clips/clip_{n:03d}.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start),
            "-t", str(CLIP_SECONDS),
            "-i", path,
            "-vf", "crop=ih*9/16:ih,scale=1080:1920",
            "-c:a", "aac",
            out
        ], check=True)
        start += CLIP_SECONDS
        n += 1
    print(f"Made {n - 1} clips in the clips folder")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py video.mp4")
    else:
        make_clips(sys.argv[1])
