import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import footballi_uploader as uploader
import renew_youtube_oauth


class YoutubeOAuthRecoveryTests(unittest.TestCase):
    def test_refresh_error_preserves_telegram_and_discovery(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(uploader, "STATE_FILE", str(Path(directory) / "state.json")), \
             patch.object(uploader, "get_youtube_client", side_effect=uploader.RefreshError("invalid_grant")), \
             patch.object(uploader.telegram.TelegramReview, "from_env", return_value=Mock()) as telegram, \
             patch.object(uploader, "fetch_live_fixtures", return_value=[]), \
             patch.object(uploader, "crawl_footballi_videos", return_value=[]) as crawl, \
             patch.object(uploader, "fetch_instagram_reels", return_value=[]), \
             patch.object(uploader, "propose_telegram_reviews") as propose, \
             patch.object(uploader, "prepare_telegram_link_requests") as prepare, \
             patch.dict("os.environ", {"FOOTBALLI_ORIGINALS_ONLY": "true"}):
            with self.assertRaisesRegex(SystemExit, "YouTube authentication failed"):
                uploader.main()
            self.assertIn("last_run", json.loads(Path(uploader.STATE_FILE).read_text(encoding="utf-8")))
        telegram.return_value.poll.assert_called_once()
        crawl.assert_called_once()
        propose.assert_called_once()
        prepare.assert_not_called()

    def test_verified_channel_required_before_exporting_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "client.json"
            config.write_text('{"installed": {"client_id": "mock"}}', encoding="utf-8")
            with patch.object(renew_youtube_oauth.InstalledAppFlow, "from_client_config") as factory, \
                 patch.object(renew_youtube_oauth, "build") as build, \
                 patch.object(renew_youtube_oauth.tempfile, "mkstemp") as create_file:
                build.return_value.channels.return_value.list.return_value.execute.return_value = {
                    "items": [{"id": "wrong-channel"}],
                }
                with self.assertRaisesRegex(ValueError, "expected YouTube channel"):
                    renew_youtube_oauth.export_token(config, "footballi-channel")
                factory.return_value.run_local_server.return_value.refresh.assert_called_once()
                create_file.assert_not_called()
