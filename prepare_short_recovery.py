"""Rebuild an already-published Instagram Short from its official source."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from googleapiclient.http import MediaFileUpload

from footballi_uploader import get_youtube_client, load_state
from media_utils import inspect_audio_file, inspect_video_file


def verified_source(state, video_id):
    entry = state.get("published_metadata", {}).get(video_id, {})
    source = urlsplit(entry.get("source_url", ""))
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
        or entry.get("category") != "SHORTS"
        or entry.get("source") != "instagram"
        or entry.get("rights_status") != "internal_authorized"
        or source.scheme != "https"
        or source.netloc not in {"instagram.com", "www.instagram.com"}
        or not re.fullmatch(r"/(?:p|reel)/[A-Za-z0-9_-]+/?", source.path)
    ):
        raise ValueError("A known, authorized Instagram Short is required")
    return source._replace(query="", fragment="").geturl()


def prepare_source(source, directory):
    template = str(directory / "source.%(ext)s")
    run = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--quiet",
         "--socket-timeout", "15", "--retries", "1", "--max-filesize", "100M",
         "-f", "bv+ba/b[audio_ext!=none]", "--merge-output-format", "mp4",
         "-o", template, source],
        capture_output=True, timeout=150,
    )
    source_files = list(directory.glob("source.*"))
    if run.returncode or len(source_files) != 1:
        raise RuntimeError("The official post did not return a complete media file")
    original = str(source_files[0])
    width, height, seconds = inspect_video_file(original)
    issue, _ = inspect_audio_file(original)
    if issue or not (height > width >= 720 and 0 < seconds <= 175):
        raise RuntimeError(f"Source is not a verified 720p Short with sound ({issue or 'dimensions'})")
    output = str(directory / "recovered.mp4")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", original,
         "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-ar", "48000", "-movflags", "+faststart", output],
        capture_output=True, check=True, timeout=100,
    )
    issue, peak = inspect_audio_file(output)
    if issue:
        raise RuntimeError(f"Rebuilt file has unverified audio ({issue})")
    print(f"[RECOVERY] Verified source and restored output: {width}x{height}, {seconds:.1f}s, {peak:.1f} dBFS")
    return output


def main():
    video_id = os.environ["RECOVER_VIDEO_ID"].strip()
    source = verified_source(load_state(), video_id)
    youtube = get_youtube_client()
    rows = youtube.videos().list(part="snippet,status,fileDetails", id=video_id).execute()["items"]
    if len(rows) != 1 or not rows[0].get("fileDetails"):
        raise RuntimeError("Could not verify the original upload as its owner")
    original = rows[0]
    streams = original["fileDetails"].get("audioStreams", [])
    print(f"[RECOVERY] Original upload reports {len(streams)} audio stream(s).")
    with tempfile.TemporaryDirectory() as work:
        output = prepare_source(source, Path(work))
        if os.getenv("STAGE_UPLOAD") != "true":
            return
        snippet = original["snippet"]
        body = {
            "snippet": {k: snippet[k] for k in ("title", "description", "categoryId", "tags") if k in snippet},
            "status": {"privacyStatus": "unlisted",
                       "selfDeclaredMadeForKids": original["status"].get("selfDeclaredMadeForKids", False)},
        }
        request = youtube.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(output, chunksize=8 * 1024 * 1024, resumable=True,
                                       mimetype="video/mp4"),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        print(f"[RECOVERY] Staged replacement: https://www.youtube.com/shorts/{response['id']}")


if __name__ == "__main__":
    main()
