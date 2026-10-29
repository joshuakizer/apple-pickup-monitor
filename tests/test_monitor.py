import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest import mock

import monitor


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MonitorTests(unittest.TestCase):
    def valid_store(self, available=()):
        return {
            "storeNumber": "R289",
            "storeName": "Brandon",
            "address": {"postalCode": "33511"},
            "partsAvailability": {
                part: {"pickupDisplay": "available" if part in available else "unavailable"}
                for part in monitor.PARTS
            },
        }

    def test_fetch_location_requests_every_part_and_returns_stores(self):
        stores = [self.valid_store()]
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(json.dumps({"body": {"stores": stores}}).encode())

        with mock.patch.object(monitor, "APPLE_OPENER", opener):
            self.assertEqual(monitor.fetch_location("33579"), stores)

        request = opener.open.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query["location"], ["33579"])
        self.assertEqual(
            [query[f"parts.{index}"][0] for index in range(len(monitor.PARTS))],
            list(monitor.PARTS),
        )

    def test_fetch_location_surfaces_apple_error(self):
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(
            json.dumps({"body": {"errorMessage": "Product(s) Invalid or not buyable"}}).encode()
        )

        with mock.patch.object(monitor, "APPLE_OPENER", opener):
            with self.assertRaisesRegex(RuntimeError, "Invalid or not buyable"):
                monitor.fetch_location("33579")

    def test_genuine_zero_stock_is_a_successful_check(self):
        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "STORES", "ALL"), \
             mock.patch.object(monitor, "fetch_location", return_value=[self.valid_store()]):
            available, seen = monitor.check()

        self.assertEqual(available, {})
        self.assertEqual(seen, {"R289"})

    def test_inventory_request_failure_is_not_treated_as_zero_stock(self):
        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "fetch_location", side_effect=RuntimeError("request failed")):
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                monitor.check()

    def test_check_rejects_missing_product_level_availability(self):
        store = self.valid_store()
        missing_part = next(iter(monitor.PARTS))
        del store["partsAvailability"][missing_part]

        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "STORES", "ALL"), \
             mock.patch.object(monitor, "fetch_location", return_value=[store]):
            with self.assertRaisesRegex(RuntimeError, "omitted product availability"):
                monitor.check()

    def test_check_rejects_malformed_product_availability(self):
        store = self.valid_store()
        part = next(iter(monitor.PARTS))
        store["partsAvailability"][part] = {"pickupDisplay": "unknown"}

        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "STORES", "ALL"), \
             mock.patch.object(monitor, "fetch_location", return_value=[store]):
            with self.assertRaisesRegex(RuntimeError, "unverifiable availability"):
                monitor.check()

    def test_check_keeps_only_watched_available_parts(self):
        part = next(iter(monitor.PARTS))
        store = self.valid_store(available={part})
        store["partsAvailability"]["UNKNOWN"] = {"pickupDisplay": "available"}

        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "STORES", "ALL"), \
             mock.patch.object(monitor, "fetch_location", return_value=[store]):
            available, seen = monitor.check()

        self.assertEqual(available, {("R289", part): "Brandon (33511)"})
        self.assertEqual(seen, {"R289"})

    def test_send_ntfy_builds_expected_request(self):
        response = FakeResponse(b"ok")
        with mock.patch.object(monitor, "NTFY_SERVER", "https://ntfy.example/"), \
             mock.patch.object(monitor, "NTFY_TOPIC", "private topic"), \
             mock.patch("monitor.urllib.request.urlopen", return_value=response) as urlopen:
            monitor.send_ntfy("Test title", "Test body", priority="default")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ntfy.example/private%20topic")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"Test body")
        self.assertEqual(request.headers["Title"], "Test title")
        self.assertEqual(request.headers["Priority"], "default")

    def test_missing_state_establishes_silent_baseline(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", return_value=(current, {"R289"})), \
             mock.patch.object(monitor, "notify") as notify:
            monitor.run_once()
            saved = monitor.load_inventory_state()

        notify.assert_not_called()
        self.assertEqual(saved, current)

    def test_unchanged_stock_does_not_notify_twice(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", return_value=(current, {"R289"})), \
             mock.patch.object(monitor, "notify") as notify:
            monitor.save_inventory_state(current)
            monitor.run_once()
            monitor.run_once()

        notify.assert_not_called()

    def test_new_stock_notifies_once_and_is_then_deduplicated(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", return_value=(current, {"R289"})), \
             mock.patch.object(monitor, "notify") as notify:
            monitor.save_inventory_state({})
            monitor.run_once()
            monitor.run_once()

        self.assertEqual(notify.call_count, 1)
        self.assertTrue(notify.call_args.kwargs["strict"])

    def test_out_of_stock_transition_allows_future_realert(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        checks = [({}, {"R289"}), (current, {"R289"})]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", side_effect=checks), \
             mock.patch.object(monitor, "notify") as notify:
            monitor.save_inventory_state(current)
            monitor.run_once()
            monitor.run_once()

        self.assertEqual(notify.call_count, 1)

    def test_expired_state_is_ignored(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor.save_inventory_state(current, path=path, now=100)
            self.assertIsNone(
                monitor.load_inventory_state(path=path, now=100 + monitor.STATE_TTL_HOURS * 3600 + 1)
            )

    def test_malformed_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{not-json")
            self.assertIsNone(monitor.load_inventory_state(path=path))

    def test_failed_check_exits_nonzero_path_and_preserves_state(self):
        part = next(iter(monitor.PARTS))
        previous = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", side_effect=RuntimeError("all requests failed")):
            monitor.save_inventory_state(previous)
            before = monitor.STATE_FILE.read_text()
            with self.assertRaisesRegex(RuntimeError, "all requests failed"):
                monitor.run(once=True)
            after = monitor.STATE_FILE.read_text()

        self.assertEqual(after, before)

    def test_once_command_returns_nonzero_for_failed_check(self):
        with mock.patch.object(monitor, "check", side_effect=RuntimeError("all requests failed")):
            self.assertEqual(monitor.main(["--once"]), 1)

    def test_once_command_returns_zero_for_verified_zero_stock(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", return_value=({}, {"R289"})):
            self.assertEqual(monitor.main(["--once"]), 0)

    def test_notification_failure_does_not_advance_state(self):
        part = next(iter(monitor.PARTS))
        current = {("R289", part): "Brandon (33511)"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(monitor, "STATE_FILE", Path(directory) / "state.json"), \
             mock.patch.object(monitor, "check", return_value=(current, {"R289"})), \
             mock.patch.object(monitor, "notify", side_effect=RuntimeError("ntfy failed")):
            monitor.save_inventory_state({})
            before = monitor.STATE_FILE.read_text()
            with self.assertRaisesRegex(RuntimeError, "ntfy failed"):
                monitor.run_once()
            after = monitor.STATE_FILE.read_text()

        self.assertEqual(after, before)

    def test_all_eight_duo_configurations_are_watched(self):
        self.assertEqual(len(monitor.PARTS), 8)
        self.assertEqual(
            set(monitor.PARTS.values()),
            {
                ("256gb", "night sky"), ("256gb", "star white"),
                ("512gb", "night sky"), ("512gb", "star white"),
                ("1tb", "night sky"), ("1tb", "star white"),
                ("2tb", "night sky"), ("2tb", "star white"),
            },
        )

    def test_workflow_is_scheduled_and_persists_state_without_changing_secrets(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/monitor.yml").read_text()
        self.assertIn("cron: '*/5 * * * *'", workflow)
        self.assertIn("actions/cache/restore@v4", workflow)
        self.assertIn("actions/cache/save@v4", workflow)
        self.assertIn('LOCATIONS: "33579"', workflow)
        self.assertIn('CAPACITIES: "ALL"', workflow)
        self.assertIn("NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}", workflow)
        self.assertIn("python3 monitor.py --test-notification", workflow)


if __name__ == "__main__":
    unittest.main()
