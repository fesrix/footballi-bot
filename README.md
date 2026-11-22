# Footballi Bot

**A scheduled Python pipeline for discovering, preparing, and publishing football videos and Shorts to YouTube.**

[![Scheduled publishing workflow](https://github.com/fesrix/footballi-bot/actions/workflows/uploader.yml/badge.svg)](https://github.com/fesrix/footballi-bot/actions/workflows/uploader.yml)

[Workflow runs](https://github.com/fesrix/footballi-bot/actions) · [Pipeline entry point](footballi_uploader.py) · [Growth report](growth_report.md)

## How it works

```mermaid
flowchart LR
    A["Discover candidates"] --> B["Filter and deduplicate"]
    B --> C["Apply upload windows and quota"]
    C --> D["Prepare media and metadata"]
    D --> E["Publish to YouTube"]
    E --> F["Update upload history"]
```

- **Discovery** collects candidates from Footballi and, when configured, Instagram. Persian text normalization and title matching help identify duplicate candidates.
- **Publishing controls** apply separate scheduling and pacing rules for videos and Shorts. The uploader checks its saved history and channel uploads before publishing.
- **Media preparation** uses FFmpeg and FFprobe to handle video and inspect its dimensions. The pipeline also prepares thumbnails, titles, descriptions, and playlist assignments.
- **Operations** include a scheduled Actions run, comment moderation, channel analytics, and a generated growth report.

## Code map

| Area | Entry point |
| --- | --- |
| Candidate discovery and source parsing | [`scrapers.py`](scrapers.py) |
| Queue, quotas, playlists, and publisher | [`footballi_uploader.py`](footballi_uploader.py) |
| FFmpeg, video inspection, and thumbnail preparation | [`media_utils.py`](media_utils.py) |
| AI-assisted ranking and metadata | [`ai_seo.py`](ai_seo.py) |
| Upload windows, pacing, and limits | [`config.py`](config.py) |
| Comment moderation and reporting | [`comment_manager.py`](comment_manager.py), [`analytics.py`](analytics.py), [`growth_report.py`](growth_report.py) |

## Run and operate

The [publisher workflow](.github/workflows/uploader.yml) runs on a 30-minute cron schedule and supports manual dispatch. An individual run can skip publication when the queue, time window, pacing rules, or quota prevents it.

The workflow uses **Python 3.12**, the packages in [`requirements.txt`](requirements.txt), FFmpeg, FFprobe, and Chrome. For local code review, install the Python dependencies with:

```bash
python -m venv .venv
# Activate .venv for your shell before installing dependencies.
python -m pip install -r requirements.txt
```

The workflow reads `YOUTUBE_TOKEN` and `CLIENT_SECRETS` for YouTube authentication, `GEMINI_API_KEY` and `ARVAN_API_KEY` for AI integrations, and `APIFY_TOKEN` for Instagram discovery. Configure credentials through GitHub Actions secrets. Treat manual dispatch and the uploader entry point as live operations when a real channel is connected.

For troubleshooting, start with the job summary and stage logs in the [Actions run](https://github.com/fesrix/footballi-bot/actions/workflows/uploader.yml), then check the [upload history](uploaded_matches.json) for the recorded state.
# Emergency editorial approvals

Originals-only publishing is active by default. Set the GitHub Actions
repository variable `FOOTBALLI_ORIGINALS_ONLY=false` only after explicitly
resuming ordinary uploads. The separate programme hold has its own setting.

### Telegram review

Create a dedicated bot in [BotFather](https://t.me/BotFather) and send it
`/start` from the reviewer's private chat. Configure `TELEGRAM_BOT_TOKEN` and
the reviewer's numeric Telegram user ID as `TELEGRAM_REVIEWER_ID` in the
repository's GitHub Actions secrets. Use a bot without an active webhook or
another update consumer. Keep the token out of chat, commits, and logs.

With originals-only mode active, the next normal run discovers media and
sends up to three new cards. Approve only when Footballi produced the video
and holds permission to publish its picture and sound on YouTube. The next
run reads the decision and checks the exact file before publishing. Rejecting
a card removes that source from consideration. Current status and the
YouTube URL are added to the original card.

To test on the live schedule, set the repository variable
`FOOTBALLI_TELEGRAM_PREVIEW_ONLY=true` before configuring the secrets.
Wait for a card and click a button, then confirm the result on the following
run. Preview approvals cannot authorize publication. Remove the variable
to resume publication and review the new production cards.

### Editorial approval file

Editorial clearance can also be entered in `approved_originals.json`. The
object keys are official Footballi video or Instagram post URLs. Each record
requires a non-empty `production_evidence` describing how the production and
rights were verified, boolean `visual_rights_confirmed` and
`audio_rights_confirmed` set to `true`, and the 64-character `sha256` of the
exact file the bot will upload. Recovery requires the hash of the recovered
output. Keep private evidence and credentials out of the repository. Use the
existing Gemini credential for the AI evidence review.

Run `python footballi_uploader.py --audit-originals-queue` to review the
queue without touching YouTube or writing state. The SHA-256 and AI checks
run on the actual upload path after file retrieval.

