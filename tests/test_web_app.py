import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import create_app
from app.automator import (
    AutomationManager,
    TWITCH_INVENTORY_URL,
    UserAutomator,
    browser_channel_candidates,
    launch_native_twitch_login,
    normalize_drop_name,
    normalize_twitch_game_url,
    screencast_emit_interval,
    screencast_options,
)
from app.config import _load_or_create_secret_key
from app.extensions import db
from app.models import DropLog, User
from app.process_lock import ProcessLock, ProcessLockError
from app.routes import _resolve_discovery_future


class WebAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

        class TestConfig:
            TESTING = True
            SECRET_KEY = "test-secret"
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_TRACK_MODIFICATIONS = False
            DATA_DIR = self.temp_dir.name
            BROWSER_DATA_DIR = os.path.join(self.temp_dir.name, "browser")
            NATIVE_LOGIN_ENABLED = True

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.temp_dir.cleanup()

    def register(self, username="tester", password="secret12"):
        return self.client.post(
            "/register",
            data={
                "username": username,
                "password": password,
                "confirm_password": password,
            },
        )

    def test_login_rejects_external_next_redirect(self):
        self.register()
        self.client.get("/logout")

        response = self.client.post(
            "/login?next=https://evil.example/collect",
            data={"username": "tester", "password": "secret12"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/")

    def test_login_rejects_browser_normalized_external_next_redirect(self):
        self.register()
        self.client.get("/logout")

        for target in (r"/\evil.example", "/%5cevil.example", "/%2fevil.example"):
            response = self.client.post(
                "/login",
                query_string={"next": target},
                data={"username": "tester", "password": "secret12"},
            )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["Location"], "/")
            self.client.get("/logout")

    def test_login_allows_local_next_redirect(self):
        self.register()
        self.client.get("/logout")

        response = self.client.post(
            "/login?next=/api/status?from=login",
            data={"username": "tester", "password": "secret12"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/api/status?from=login")

    def test_token_only_account_is_reported_as_saved(self):
        self.register()
        response = self.client.post(
            "/api/import-token",
            json={"auth_token": "stored-token"},
        )
        self.assertEqual(response.status_code, 200)

        status = self.client.get("/api/status").get_json()
        self.assertTrue(status["twitch_saved"])

    def test_native_login_requires_authentication(self):
        response = self.client.post(
            "/api/native-login",
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 302)

    def test_dashboard_hides_native_login_when_disabled(self):
        self.register()
        self.app.config["NATIVE_LOGIN_ENABLED"] = False

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"id=\"nativeLoginBtn\"", response.data)
        self.assertIn(b"Connect with an auth token below", response.data)

    def test_native_login_requires_local_same_origin_request(self):
        self.register()

        response = self.client.post(
            "/api/native-login",
            headers={"Origin": "https://evil.example"},
        )

        self.assertEqual(response.status_code, 403)

    def test_native_login_launches_for_authenticated_local_user(self):
        self.register()
        manager = MagicMock()
        manager.open_native_login_for_user.return_value = (True, "Microsoft Edge")

        with patch("app.routes.AutomationManager.get", return_value=manager):
            response = self.client.post(
                "/api/native-login",
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["browser"], "Microsoft Edge")
        manager.open_native_login_for_user.assert_called_once()

    def test_settings_reject_invalid_values_with_json_error(self):
        self.register()

        response = self.client.post(
            "/api/settings",
            json={"auto_claim": False, "check_interval": None},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("check_interval", response.get_json()["error"])

        settings = self.client.get("/api/settings").get_json()
        self.assertTrue(settings["auto_claim"])
        self.assertEqual(settings["check_interval"], 60)

    def test_settings_accept_valid_typed_values(self):
        self.register()
        response = self.client.post(
            "/api/settings",
            json={
                "auto_claim": False,
                "check_interval": 120,
                "screencast_quality": 70,
                "screencast_max_fps": 4,
            },
        )

        self.assertEqual(response.status_code, 200)
        settings = self.client.get("/api/settings").get_json()
        self.assertEqual(
            settings,
            {
                "auto_claim": False,
                "check_interval": 120,
                "screencast_quality": 70,
                "screencast_max_fps": 4,
            },
        )

    def test_watch_target_rejects_non_twitch_url(self):
        self.register()

        response = self.client.post(
            "/api/watch-targets",
            json={"game_name": "Example", "game_url": "http://db:5432/private"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Twitch", response.get_json()["error"])
        self.assertEqual(self.client.get("/api/watch-targets").get_json(), [])

    def test_watch_target_normalizes_twitch_directory_url(self):
        self.register()
        response = self.client.post(
            "/api/watch-targets",
            json={
                "game_name": "Rust",
                "game_url": "https://twitch.tv/directory/category/rust?tl=drops#top",
            },
        )

        self.assertEqual(response.status_code, 200)
        targets = self.client.get("/api/watch-targets").get_json()
        self.assertEqual(
            targets[0]["game_url"],
            "https://www.twitch.tv/directory/category/rust?tl=drops",
        )

    def test_claim_transitions_existing_history_without_hardcoded_game(self):
        self.register()
        with self.app.app_context():
            user = User.query.filter_by(username="tester").one()
            db.session.add(DropLog(
                user_id=user.id,
                drop_name="Example Reward",
                game=None,
                status="in_progress",
                progress=75,
            ))
            db.session.commit()

            automator = UserAutomator(user.id, "unused", MagicMock(), self.app)
            automator._persist_drops(
                [],
                [{"name": "Example Reward 100% of 2 hours"}],
            )

            rows = DropLog.query.filter_by(user_id=user.id).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "claimed")
            self.assertEqual(rows[0].progress, 100)
            self.assertIsNone(rows[0].game)
            self.assertIsNotNone(rows[0].claimed_at)

    def test_discovery_timeout_cancels_background_work(self):
        future = MagicMock()
        future.result.side_effect = TimeoutError()

        with self.app.app_context():
            result, error_response = _resolve_discovery_future(future, "Game")

        response, status = error_response
        self.assertIsNone(result)
        self.assertEqual(status, 504)
        self.assertIn("timed out", response.get_json()["error"])
        future.cancel.assert_called_once_with()

    def test_watch_target_rejects_malformed_port_with_json_error(self):
        self.register()

        response = self.client.post(
            "/api/watch-targets",
            json={
                "game_name": "Rust",
                "game_url": "https://www.twitch.tv:not-a-port/directory/category/rust",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("HTTPS Twitch", response.get_json()["error"])


class LifecycleTestCase(unittest.TestCase):
    def test_stop_cancels_main_task_without_marking_thread_stopped_early(self):
        socketio = MagicMock()
        app = MagicMock()
        automator = UserAutomator(1, "unused", socketio, app)
        automator.running = True
        automator._loop = MagicMock()
        automator._loop.is_running.return_value = True
        automator._loop.call_soon_threadsafe.side_effect = lambda callback: callback()
        automator._main_task = MagicMock()

        automator.stop()

        self.assertTrue(automator._stop.is_set())
        self.assertTrue(automator.running)
        automator._main_task.cancel.assert_called_once_with()
        automator._loop.stop.assert_not_called()

    def test_manager_serializes_duplicate_start(self):
        class FakeAutomator:
            def __init__(self, *args, **kwargs):
                self.alive = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def stop(self):
                pass

        with tempfile.TemporaryDirectory() as data_dir:
            app = MagicMock()
            app.config = {"BROWSER_DATA_DIR": data_dir}
            manager = AutomationManager(MagicMock(), app)
            with patch("app.automator.UserAutomator", FakeAutomator):
                self.assertTrue(manager.start_for_user(7))
                self.assertFalse(manager.start_for_user(7))

    def test_manager_blocks_start_while_native_login_browser_is_open(self):
        app = MagicMock()
        app.config = {"BROWSER_DATA_DIR": "unused"}
        manager = AutomationManager(MagicMock(), app)
        process = MagicMock()
        process.poll.return_value = None
        manager._native_login_processes[7] = process

        self.assertFalse(manager.start_for_user(7))

    def test_manager_blocks_start_while_native_login_is_launching(self):
        app = MagicMock()
        app.config = {"BROWSER_DATA_DIR": "unused"}
        manager = AutomationManager(MagicMock(), app)
        manager._native_login_starting.add(7)

        self.assertFalse(manager.start_for_user(7))

    def test_missing_campaign_records_and_target_url_clear_stale_completion(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator._completed_games.add("Warframe")
        automator._load_watch_targets = MagicMock(
            return_value=[{"game_name": "Warframe"}]
        )

        automator._detect_completed_games(campaigns=[])

        self.assertNotIn("Warframe", automator._completed_games)

    def test_explicit_campaign_completion_marks_only_named_game(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator._completed_games.add("Destiny 2")
        automator._load_watch_targets = MagicMock(
            return_value=[
                {
                    "game_name": "Warframe",
                    "game_url": "https://www.twitch.tv/directory/category/warframe",
                },
                {
                    "game_name": "Destiny 2",
                    "game_url": "https://www.twitch.tv/directory/category/destiny-2",
                },
            ]
        )

        automator._detect_completed_games(
            campaigns=[
                {"gamePath": "/directory/category/warframe", "complete": True},
                {"gamePath": "/directory/category/destiny-2", "complete": False},
            ],
        )

        self.assertEqual(automator._completed_games, {"Warframe"})

    def test_mixed_campaign_records_keep_game_active(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator._completed_games.add("Warframe")
        automator._load_watch_targets = MagicMock(
            return_value=[{
                "game_name": "Warframe",
                "game_url": "https://www.twitch.tv/directory/category/warframe",
            }]
        )

        automator._detect_completed_games(campaigns=[
            {"gamePath": "/directory/category/warframe", "complete": True},
            {"gamePath": "/directory/category/warframe", "complete": False},
        ])

        self.assertNotIn("Warframe", automator._completed_games)

    def test_completed_other_category_does_not_mark_selected_game(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator._load_watch_targets = MagicMock(
            return_value=[{
                "game_name": "Warframe",
                "game_url": "https://www.twitch.tv/directory/category/warframe",
            }]
        )

        automator._detect_completed_games(campaigns=[
            {"gamePath": "/directory/category/destiny-2", "complete": True},
        ])

        self.assertNotIn("Warframe", automator._completed_games)


class InventoryPageTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_low_quality_clicks_visible_lowest_quality_control(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        settings_button = MagicMock()
        settings_button.click = AsyncMock()
        quality_button = MagicMock()
        quality_button.click = AsyncMock()
        auto_option = MagicMock()
        low_option = MagicMock()
        low_option.click = AsyncMock()
        automator.page = MagicMock()
        automator.page.query_selector = AsyncMock(
            side_effect=[settings_button, quality_button]
        )
        automator.page.query_selector_all = AsyncMock(
            return_value=[auto_option, low_option]
        )

        with patch("app.automator.asyncio.sleep", new=AsyncMock()):
            await automator._set_low_quality()

        automator.page.query_selector_all.assert_awaited_once_with(
            '[data-a-target="player-settings-submenu-quality-option"]'
        )
        low_option.click.assert_awaited_once_with()

    async def test_drop_check_uses_separate_page_and_honors_disabled_auto_claim(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        stream_page = MagicMock(name="stream_page")
        inventory_page = MagicMock(name="inventory_page")
        inventory_page.goto = AsyncMock()
        inventory_page.close = AsyncMock()
        inventory_page.query_selector_all = AsyncMock()
        inventory_page.evaluate = AsyncMock(
            side_effect=[
                0,
                None,
                {"items": [], "campaigns": []},
            ]
        )
        automator.page = stream_page
        automator.context = MagicMock()
        automator.context.new_page = AsyncMock(return_value=inventory_page)
        automator._get_auto_claim = MagicMock(return_value=False)
        automator._detect_completed_games = MagicMock()
        automator._persist_drops = MagicMock()
        automator._update_status = MagicMock()

        with patch("app.automator.asyncio.sleep", new=AsyncMock()):
            await automator._check_and_claim_drops()

        self.assertIs(automator.page, stream_page)
        inventory_page.goto.assert_awaited_once_with(
            TWITCH_INVENTORY_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        inventory_page.close.assert_awaited_once_with()
        inventory_page.query_selector_all.assert_not_awaited()

    async def test_mature_content_gate_is_not_treated_as_offline(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        gate = MagicMock()
        gate.text_content = AsyncMock(return_value="Mature content warning")
        video = MagicMock()
        automator.page = MagicMock()
        automator.page.url = "https://www.twitch.tv/example"
        automator.page.query_selector = AsyncMock(side_effect=[gate, video])
        automator._accept_mature_content = AsyncMock()

        self.assertTrue(await automator._is_stream_live())
        automator._accept_mature_content.assert_awaited_once_with()

    async def test_completed_current_game_switches_without_live_check(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator.status.update({
            "watching": "https://www.twitch.tv/example",
            "watching_game": "Warframe",
            "stream_name": "example",
        })
        automator._completed_games.add("Warframe")
        automator._stop_watch_timer = MagicMock()
        automator._update_status = MagicMock()
        automator._is_stream_live = AsyncMock(return_value=True)
        automator._find_best_stream = AsyncMock()
        automator._sleep = AsyncMock()

        await automator._watch_loop_cycle()

        automator._stop_watch_timer.assert_called_once_with()
        automator._is_stream_live.assert_not_awaited()
        automator._find_best_stream.assert_awaited_once_with()
        automator._update_status.assert_called_once_with(
            watching=None,
            watching_game=None,
            stream_name=None,
            message="Warframe campaign complete — finding another…",
        )

    async def test_active_live_game_keeps_current_stream(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator.status.update({
            "watching": "https://www.twitch.tv/example",
            "watching_game": "Warframe",
            "stream_name": "example",
        })
        automator._is_stream_live = AsyncMock(return_value=True)
        automator._update_watch_time = MagicMock()
        automator._update_status = MagicMock()
        automator._find_best_stream = AsyncMock()
        automator._sleep = AsyncMock()
        automator._get_check_interval = MagicMock(return_value=30)

        await automator._watch_loop_cycle()

        automator._is_stream_live.assert_awaited_once_with()
        automator._find_best_stream.assert_not_awaited()
        automator._sleep.assert_awaited_once_with(30)

    async def test_stream_selection_skips_cards_without_drops_enabled(self):
        automator = UserAutomator(1, "unused", MagicMock(), MagicMock())
        automator._load_watch_targets = MagicMock(return_value=[{
            "game_name": "Example",
            "game_url": "https://www.twitch.tv/directory/category/example",
        }])
        automator.page = MagicMock()
        automator.page.evaluate = AsyncMock(return_value=["/eligible-channel"])
        automator.page.query_selector = AsyncMock(return_value=None)
        automator._goto = AsyncMock()
        automator._is_stream_live = AsyncMock(return_value=True)
        automator._start_watching = AsyncMock()
        automator._update_status = MagicMock()

        with patch("app.automator.asyncio.sleep", new=AsyncMock()):
            await automator._find_best_stream()

        evaluate_args = automator.page.evaluate.await_args.args
        self.assertFalse(evaluate_args[1])
        automator._goto.assert_any_await("https://www.twitch.tv/eligible-channel")
        automator._start_watching.assert_awaited_once_with(
            "eligible-channel",
            "https://www.twitch.tv/eligible-channel",
            "Example",
        )


class ConfigurationTestCase(unittest.TestCase):
    def test_process_lock_rejects_second_server_for_same_data_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            path = os.path.join(data_dir, ".server.lock")
            with ProcessLock(path):
                with self.assertRaises(ProcessLockError):
                    with ProcessLock(path):
                        pass

    def test_drop_name_strips_progress_without_losing_numbers_in_reward(self):
        self.assertEqual(
            normalize_drop_name("100 CHRONO TOKENS - 2\n1% of 3 hours"),
            "100 CHRONO TOKENS - 2",
        )
        self.assertEqual(
            normalize_drop_name("Thunderous Chuckle Emoji 1% of 2 hours"),
            "Thunderous Chuckle Emoji",
        )
        self.assertEqual(normalize_drop_name("1% of 2 hours"), "Drop")

    def test_native_login_uses_dedicated_profile_and_no_shell(self):
        process = MagicMock()
        with tempfile.TemporaryDirectory() as data_dir, patch(
            "app.automator.find_native_browser",
            return_value=("Microsoft Edge", r"C:\Program Files\Edge\msedge.exe", "msedge"),
        ), patch("app.automator.subprocess.Popen", return_value=process) as popen:
            browser_name, channel, launched = launch_native_twitch_login(data_dir)

        self.assertEqual(browser_name, "Microsoft Edge")
        self.assertEqual(channel, "msedge")
        self.assertIs(launched, process)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], r"C:\Program Files\Edge\msedge.exe")
        self.assertIn(f"--user-data-dir={os.path.abspath(data_dir)}", command)
        self.assertIn("--disable-background-mode", command)

    def test_branded_browser_channels_precede_bundled_chromium(self):
        self.assertEqual(
            browser_channel_candidates(),
            ("msedge", "chrome", None),
        )

    def test_configured_browser_channel_takes_priority_without_duplicates(self):
        self.assertEqual(
            browser_channel_candidates("msedge"),
            ("msedge",),
        )

    def test_generated_secret_is_persisted(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with patch.dict(os.environ, {}, clear=True):
                first = _load_or_create_secret_key(data_dir)
                second = _load_or_create_secret_key(data_dir)

        self.assertEqual(first, second)
        self.assertGreater(len(first), 40)

    def test_url_normalizer_rejects_non_directory_twitch_pages(self):
        with self.assertRaises(ValueError):
            normalize_twitch_game_url("https://www.twitch.tv/user")

    def test_screencast_fps_is_applied_as_an_emit_interval(self):
        self.assertEqual(screencast_emit_interval(1), 1.0)
        self.assertAlmostEqual(screencast_emit_interval(3), 1 / 3)
        self.assertEqual(screencast_emit_interval(10), 0.1)

    def test_screencast_cdp_options_do_not_skip_static_page_frames(self):
        options = screencast_options(70)

        self.assertEqual(options["quality"], 70)
        self.assertEqual(options["everyNthFrame"], 1)


if __name__ == "__main__":
    unittest.main()
