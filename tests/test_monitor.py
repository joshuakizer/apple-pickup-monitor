import io
import json
from pathlib import Path
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
    def test_fetch_location_requests_every_part_and_returns_stores(self):
        stores = [{"storeNumber": "R289", "storeName": "Brandon", "partsAvailability": {}}]
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

    def test_check_keeps_only_watched_available_parts(self):
        part = next(iter(monitor.PARTS))
        stores = [{
            "storeNumber": "R289",
            "storeName": "Brandon",
            "address": {"postalCode": "33511"},
            "partsAvailability": {
                part: {"pickupDisplay": "available"},
                "UNKNOWN": {"pickupDisplay": "available"},
            },
        }]

        with mock.patch.object(monitor, "LOCATIONS", ["33579"]), \
             mock.patch.object(monitor, "STORES", "ALL"), \
             mock.patch.object(monitor, "fetch_location", return_value=stores):
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

    def test_one_shot_without_stock_does_not_notify(self):
        with mock.patch.object(monitor, "check", return_value=({}, {"R289"})), \
             mock.patch.object(monitor, "notify") as notify:
            monitor.run(once=True)
        notify.assert_not_called()

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

    def test_workflow_is_scheduled_for_requested_zip_and_preserves_secret_reference(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/monitor.yml").read_text()
        self.assertIn("cron: '*/5 * * * *'", workflow)
        self.assertIn('LOCATIONS: "33579"', workflow)
        self.assertIn('CAPACITIES: "ALL"', workflow)
        self.assertIn("NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}", workflow)


if __name__ == "__main__":
    unittest.main()
