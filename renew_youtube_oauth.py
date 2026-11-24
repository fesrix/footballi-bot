"""Authorize the Footballi bot locally and save a replacement GitHub secret."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def export_token(client_file, expected_channel):
    config = json.loads(client_file.read_text(encoding="utf-8-sig"))
    if not config.get("installed"):
        raise ValueError("Use a Desktop app OAuth client JSON from Google Cloud.")
    flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, timeout_seconds=300,
        authorization_prompt_message=None, access_type="offline", prompt="consent",
        success_message="Google authorization completed. Return to the local terminal.",
    )
    if not credentials.refresh_token or not credentials.has_scopes(SCOPES):
        raise ValueError("Google did not grant offline access and all required YouTube scopes.")
    credentials.refresh(Request())
    youtube = build("youtube", "v3", credentials=credentials)
    channels = youtube.channels().list(part="id", mine=True).execute().get("items", [])
    if not any(channel.get("id") == expected_channel for channel in channels):
        raise ValueError("The authorized account did not select the expected YouTube channel.")
    fd, name = tempfile.mkstemp(prefix="footballi-youtube-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(credentials.to_json())
    return Path(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_file", type=Path, help="Google Cloud Desktop app OAuth JSON")
    parser.add_argument("--channel-id", required=True, help="Expected YouTube channel ID")
    args = parser.parse_args()
    try:
        path = export_token(args.client_file, args.channel_id)
    except ValueError as exc:
        parser.exit(1, f"OAuth configuration or channel verification failed ({type(exc).__name__}).\n"
                    "Use Desktop app credentials, grant all scopes and select the correct channel.\n")
    except Exception as exc:
        parser.exit(1, f"OAuth authorization failed ({type(exc).__name__}).\n")
    print(f"Channel verified. Replacement secret saved in: {path}")
    print("Paste its contents into GitHub Actions secret YOUTUBE_TOKEN, then delete the temporary file.")


if __name__ == "__main__":
    main()
