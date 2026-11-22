"""Rebuild an already-published Instagram Short from its official source."""
import os
import originals_policy as originals
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from googleapiclient.http import MediaFileUpload

from footballi_uploader import get_youtube_client, load_state
from media_utils import download_file, inspect_audio_file, inspect_video_file
from programme_hold import blocked_candidate_programme


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


def download_tracks(picture_url, audio_url, directory):
    paths = [str(directory / name) for name in ("picture.mp4", "sound.m4a")]
    for url, path in zip((picture_url, audio_url), paths):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".cdninstagram.com")
                or parsed.netloc != parsed.hostname):
            raise ValueError("Separate tracks must be served by the Instagram media CDN")
        query = "&".join(part for part in parsed.query.split("&")
                         if not part.startswith(("bytestart=", "byteend=")))
        if not download_file(parsed._replace(query=query, fragment="").geturl(), path):
            raise RuntimeError("The official CDN did not deliver a complete track")
    return paths


def download_post(source, directory):
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
    return str(source_files[0])


def prepare_source(source, directory, picture_url="", audio_url=""):
    if bool(picture_url) != bool(audio_url):
        raise ValueError("Provide both media tracks, or leave both empty")
    if picture_url and audio_url:
        picture, sound = download_tracks(picture_url, audio_url, directory)
        inputs = ["-i", picture, "-i", sound]
        audio_stream = "1:a:0"
    else:
        picture = sound = download_post(source, directory)
        inputs = ["-i", picture]
        audio_stream = "0:a:0"
    width, height, seconds = inspect_video_file(picture)
    issue, _ = inspect_audio_file(sound)
    if issue or not (height > width >= 720 and 0 < seconds <= 175):
        raise RuntimeError(f"Source is not a verified 720p Short with sound ({issue or 'dimensions'})")
    output = str(directory / "recovered.mp4")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", *inputs,
         "-map", "0:v:0", "-map", audio_stream, "-c:v", "libx264", "-preset", "veryfast",
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
    state = load_state()
    source = verified_source(state, video_id)
    youtube = get_youtube_client()
    rows = youtube.videos().list(part="snippet,status,fileDetails", id=video_id).execute()["items"]
    if len(rows) != 1 or not rows[0].get("fileDetails"):
        raise RuntimeError("Could not verify the original upload as its owner")
    original = rows[0]
    candidate = {**state["published_metadata"][video_id], "source_url": source}
    if originals.originals_only():
        _, reason = originals.admission(candidate, originals.load_approvals())
        reason = reason or originals.prohibited(candidate, original.get("snippet"),
                                                original["fileDetails"].get("fileName"))
        if reason:
            print(f"[ORIGINALS] recovery held | {reason}")
            return
    streams = original["fileDetails"].get("audioStreams", [])
    print(f"[RECOVERY] Original upload reports {len(streams)} audio stream(s).")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    inputs = event.get("inputs", {})
    programme = blocked_candidate_programme(
        state["published_metadata"][video_id], source, original.get("snippet"),
        original["fileDetails"].get("fileName"), inputs.get("picture_url"), inputs.get("audio_url"),
    )
    if programme:
        print(f"[PROGRAMME-HOLD] {programme}: skipped recovery of {video_id}")
        return
    with tempfile.TemporaryDirectory() as work:
        output = prepare_source(source, Path(work), inputs.get("picture_url") or "",
                                inputs.get("audio_url") or "")
        if os.getenv("STAGE_UPLOAD") != "true":
            return
        snippet = original["snippet"]
        body = {
            "snippet": {k: snippet[k] for k in ("title", "description", "categoryId", "tags") if k in snippet},
            "status": {"privacyStatus": "unlisted",
                       "selfDeclaredMadeForKids": original["status"].get("selfDeclaredMadeForKids", False)},
        }
        if originals.originals_only():
            decision = originals.verify_upload(candidate, output, body["snippet"])
            print(f"[ORIGINALS] recovery | {decision}")
            if decision != "approved":
                return
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
