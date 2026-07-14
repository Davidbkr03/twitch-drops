import os
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
