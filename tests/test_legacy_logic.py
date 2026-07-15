import os
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import twitch_drop_automator as legacy


class GeneralDropCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_campaign_data_is_not_complete(self):
        self.assertFalse(await legacy.are_all_general_drops_complete(object(), []))

    async def test_missing_progress_without_claim_confirmation_is_not_complete(self):
        with (
            patch.object(legacy, "get_general_drops_progress_map", AsyncMock(return_value={})),
            patch.object(legacy, "is_general_item_claimed_on_inventory", AsyncMock(return_value=None)),
        ):
            complete = await legacy.are_all_general_drops_complete(
                object(),
                [{"item": "Example Reward", "alias": "example"}],
            )

        self.assertFalse(complete)

    async def test_missing_progress_with_claim_confirmation_is_complete(self):
        with (
            patch.object(legacy, "get_general_drops_progress_map", AsyncMock(return_value={})),
            patch.object(legacy, "is_general_item_claimed_on_inventory", AsyncMock(return_value=True)),
        ):
            complete = await legacy.are_all_general_drops_complete(
                object(),
                [{"item": "Example Reward", "alias": "example"}],
            )

        self.assertTrue(complete)

    async def test_incomplete_progress_is_not_complete(self):
        with patch.object(
            legacy,
            "get_general_drops_progress_map",
            AsyncMock(return_value={"Example Reward": 75}),
        ):
            complete = await legacy.are_all_general_drops_complete(
                object(),
                [{"item": "Example Reward", "alias": "example"}],
            )

        self.assertFalse(complete)

    async def test_all_reported_progress_complete_is_complete(self):
        with patch.object(
            legacy,
            "get_general_drops_progress_map",
            AsyncMock(return_value={"Example Reward": 100}),
        ):
            complete = await legacy.are_all_general_drops_complete(
                object(),
                [{"item": "Example Reward", "alias": "example"}],
            )

        self.assertTrue(complete)


class BrowserProfileLockTests(unittest.TestCase):
    def test_cleanup_removes_only_singleton_files(self):
        with tempfile.TemporaryDirectory() as profile_dir:
            singleton_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
            for name in (*singleton_names, "Preferences"):
                with open(os.path.join(profile_dir, name), "w", encoding="utf-8") as handle:
                    handle.write("test")

            removed = legacy._cleanup_stale_browser_profile_locks(profile_dir)

            self.assertCountEqual(removed, singleton_names)
            self.assertTrue(os.path.exists(os.path.join(profile_dir, "Preferences")))


class ClaimConsoleLoggingTests(unittest.TestCase):
    def test_console_handler_is_attached_once_per_page(self):
        class FakePage:
            def __init__(self):
                self.handlers = []

            def on(self, event, handler):
                self.handlers.append((event, handler))

        page = FakePage()

        self.assertTrue(legacy._attach_claim_console_logging(page))
        self.assertFalse(legacy._attach_claim_console_logging(page))
        self.assertEqual([event for event, _ in page.handlers], ["console"])


class LegacyNameResolutionRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_partial_general_progress_update_uses_module_regex(self):
        cache = {
            "in_progress": [{"type": "general", "item": "Example Reward"}],
            "not_started": [],
            "completed": [],
            "last_updated": None,
        }

        with patch.object(legacy, "cached_drops_data", cache):
            legacy.update_cached_drops_data(None, {"Example Reward": 42})

        self.assertEqual(cache["in_progress"][0]["progress"], 42)

    async def test_claimed_days_delegates_with_normalized_streamer_name(self):
        implementation = AsyncMock(return_value=3)
        with patch.object(legacy, "_get_claimed_days_for_streamer_impl", implementation):
            days = await legacy.get_claimed_days_for_streamer(object(), "  ExampleStreamer  ")

        self.assertEqual(days, 3)
        implementation.assert_awaited_once_with(
            ANY,
            "  ExampleStreamer  ",
            "examplestreamer",
        )

    async def test_general_poll_checks_claimed_streamers_without_local_helper(self):
        class FakePage:
            wait_for_selector = AsyncMock(return_value=None)
            wait_for_timeout = AsyncMock(return_value=None)

        completed_streamers = set()
        claimed_check = AsyncMock(return_value=2)

        async def stop_after_cycle(_seconds):
            legacy.EXIT_EVENT.set()

        legacy.EXIT_EVENT.clear()
        try:
            with (
                patch.object(legacy, "goto_with_exit", AsyncMock(return_value=None)),
                patch.object(legacy, "maybe_accept_cookies", AsyncMock(return_value=None)),
                patch.object(legacy, "get_general_drops_progress_map", AsyncMock(return_value={})),
                patch.object(
                    legacy,
                    "fetch_facepunch_drops",
                    AsyncMock(
                        return_value={
                            "streamer": [{"streamer": "ExampleStreamer", "is_live": True, "item": "Reward"}],
                            "general": [],
                            "fetch_failed": False,
                        }
                    ),
                ),
                patch.object(legacy, "get_claimed_days_for_streamer", claimed_check),
                patch.object(legacy.asyncio, "sleep", AsyncMock(side_effect=stop_after_cycle)),
            ):
                result = await legacy.poll_general_until_complete_or_streamer_available(
                    object(),
                    FakePage(),
                    "General Reward",
                    completed_streamers,
                )
        finally:
            legacy.EXIT_EVENT.clear()

        self.assertEqual(result, (False, False))
        self.assertIn("examplestreamer", completed_streamers)
        claimed_check.assert_awaited_once()


class LegacyTargetSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_rust_detection_uses_exact_game_identity(self):
        self.assertTrue(legacy.is_rust_game_preference({
            "game": "Rust",
            "game_url": "https://www.twitch.tv/directory/category/rust",
        }))
        self.assertFalse(legacy.is_rust_game_preference({
            "game": "Trust No One",
            "game_url": "https://www.twitch.tv/directory/category/trust-no-one",
        }))
        self.assertFalse(legacy.is_rust_game_preference({
            "game": "Rusty Lake Paradise",
            "game_url": "https://www.twitch.tv/directory/category/rusty-lake-paradise",
        }))

    def test_selected_streamer_login_does_not_fuzzy_match_another_channel(self):
        game = {"streamers": {"streamerone": True}}

        self.assertTrue(legacy.is_streamer_allowed_for_game_preference(
            game,
            "StreamerOne",
            "https://www.twitch.tv/streamerone",
        ))
        self.assertFalse(legacy.is_streamer_allowed_for_game_preference(
            game,
            "StreamerTwo",
            "https://www.twitch.tv/streamertwo",
        ))

    def test_legacy_game_url_normalizer_rejects_external_or_local_urls(self):
        self.assertEqual(
            legacy._normalize_game_directory_url("rust"),
            "https://www.twitch.tv/directory/category/rust",
        )
        for value in (
            "http://www.twitch.tv/directory/category/rust",
            "https://evil.example/directory/category/rust",
            "https://127.0.0.1/directory/category/rust",
        ):
            self.assertEqual(legacy._normalize_game_directory_url(value), "")

    def test_legacy_channel_parser_rejects_external_or_reserved_urls(self):
        self.assertEqual(
            legacy._extract_channel_login("https://www.twitch.tv/Example_Channel"),
            "example_channel",
        )
        self.assertIsNone(legacy._extract_channel_login("https://evil.example/channel"))
        self.assertIsNone(legacy._extract_channel_login("https://www.twitch.tv/directory"))

    async def test_non_drops_streams_are_never_selected(self):
        enabled = [{
            "game": "Example",
            "game_url": "https://www.twitch.tv/directory/category/example",
        }]
        non_drops = [{
            "streamer": "popular",
            "stream_url": "https://www.twitch.tv/popular",
            "has_drops": False,
            "viewer_score": 1000,
        }]

        with patch.object(
            legacy,
            "fetch_live_drops_streamers_for_game",
            AsyncMock(return_value=non_drops),
        ):
            self.assertIsNone(await legacy.pick_live_stream_from_enabled_games(object(), enabled))

    async def test_rust_picker_reads_drops_from_accessible_tag_label(self):
        link = MagicMock()
        link.get_attribute = AsyncMock(return_value="/preferred")
        tag = MagicMock()
        tag.inner_text = AsyncMock(return_value="")
        tag.get_attribute = AsyncMock(
            side_effect=lambda name: "Tag, DropsEnabled" if name == "aria-label" else None
        )
        card = MagicMock()
        card.query_selector = AsyncMock(return_value=link)
        card.query_selector_all = AsyncMock(return_value=[tag])
        page = MagicMock()
        page.query_selector_all = AsyncMock(return_value=[card])
        page.wait_for_timeout = AsyncMock()
        page.close = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)

        with (
            patch.object(legacy, "goto_with_exit", AsyncMock()),
            patch.object(legacy, "maybe_accept_cookies", AsyncMock()),
        ):
            selected = await legacy.pick_live_rust_stream_with_drops(
                context,
                preferred_streamers=["preferred"],
            )

        self.assertEqual(selected, "https://www.twitch.tv/preferred")

    async def test_drops_stream_beats_ineligible_higher_viewer_channel(self):
        enabled = [{
            "game": "Example",
            "game_url": "https://www.twitch.tv/directory/category/example",
        }]
        streams = [
            {
                "streamer": "popular",
                "stream_url": "https://www.twitch.tv/popular",
                "has_drops": False,
                "viewer_score": 1000,
            },
            {
                "streamer": "eligible",
                "stream_url": "https://www.twitch.tv/eligible",
                "has_drops": True,
                "viewer_score": 10,
            },
        ]

        with patch.object(
            legacy,
            "fetch_live_drops_streamers_for_game",
            AsyncMock(return_value=streams),
        ):
            selected = await legacy.pick_live_stream_from_enabled_games(object(), enabled)

        self.assertEqual(selected["streamer"], "eligible")

    async def test_legacy_playback_stops_when_mature_gate_cannot_clear(self):
        page = MagicMock()
        page.wait_for_selector = AsyncMock()

        with patch.object(
            legacy,
            "accept_mature_content_gate",
            AsyncMock(return_value=False),
        ):
            self.assertFalse(await legacy.ensure_stream_playing(page))

        page.wait_for_selector.assert_not_awaited()

    async def test_legacy_playback_requires_a_loaded_video(self):
        page = MagicMock()
        page.wait_for_selector = AsyncMock(side_effect=TimeoutError)
        page.get_attribute = AsyncMock(return_value="0")
        page.hover = AsyncMock(side_effect=RuntimeError)
        page.query_selector = AsyncMock(return_value=None)

        with patch.object(
            legacy,
            "accept_mature_content_gate",
            AsyncMock(return_value=True),
        ):
            self.assertFalse(await legacy.ensure_stream_playing(page))

    async def test_selected_stream_must_match_channel_game_and_drops(self):
        target = {
            "stream_url": "https://www.twitch.tv/expected",
            "game_url": "https://www.twitch.tv/directory/category/example",
        }
        metadata = {
            "login": "expected",
            "game_url": "https://www.twitch.tv/directory/game/example",
            "drops_enabled": True,
        }

        with patch.object(
            legacy,
            "read_twitch_channel_metadata",
            AsyncMock(return_value=metadata),
        ):
            self.assertTrue(await legacy.selected_stream_matches_target(object(), target))

        metadata["login"] = "redirected"
        with patch.object(
            legacy,
            "read_twitch_channel_metadata",
            AsyncMock(return_value=metadata),
        ):
            self.assertFalse(await legacy.selected_stream_matches_target(object(), target))

    async def test_selected_game_cycle_stops_when_video_stalls(self):
        target = {
            "game": "Example",
            "streamer": "expected",
            "stream_url": "https://www.twitch.tv/expected",
            "game_url": "https://www.twitch.tv/directory/category/example",
        }
        page = MagicMock()
        page.close = AsyncMock()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)

        with (
            patch.object(
                legacy,
                "pick_live_stream_from_enabled_games",
                AsyncMock(return_value=target),
            ),
            patch.object(legacy, "goto_with_exit", AsyncMock()),
            patch.object(legacy, "maybe_accept_cookies", AsyncMock()),
            patch.object(legacy, "ensure_stream_playing", AsyncMock(return_value=True)),
            patch.object(
                legacy,
                "selected_stream_matches_target",
                AsyncMock(return_value=True),
            ),
            patch.object(legacy, "ensure_live_video_playing", AsyncMock(return_value=False)),
            patch.object(legacy, "set_low_quality", AsyncMock()),
            patch.object(legacy, "send_notification"),
            patch.object(legacy, "update_current_working_item"),
        ):
            result = await legacy.watch_selected_games_cycle(
                context,
                object(),
                [{"game": "Example"}],
            )

        self.assertFalse(result)
        page.close.assert_awaited_once_with()


class LegacyWebApiTests(unittest.TestCase):
    def test_streamer_endpoint_rejects_external_game_url(self):
        app, _ = legacy.create_web_app()
        client = app.test_client()

        response = client.get(
            "/api/games/streamers",
            query_string={"game_url": "https://evil.example/directory/category/rust"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main()
