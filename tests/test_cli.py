import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from threads_bot import cli
from threads_bot.client import ThreadsAPIError
from threads_bot.state import State


class CliPostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.queue_path = root / "queue.jsonl"
        self.state_path = root / "posted.json"
        self.queue_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {"id": "a", "text": "1件目", "scheduled_at": "2020-01-01T09:00"},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"id": "b", "text": "2件目", "scheduled_at": "2020-01-02T09:00"},
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.env = {
            "THREADS_USER_ID": "123",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_QUEUE_PATH": str(self.queue_path),
            "THREADS_STATE_PATH": str(self.state_path),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, publish_side_effect=None):
        with mock.patch.dict("os.environ", self.env, clear=True), mock.patch(
            "threads_bot.cli.publish_item"
        ) as publish:
            if publish_side_effect is not None:
                publish.side_effect = publish_side_effect
            else:
                publish.return_value = mock.Mock(post_id="p1", permalink=None)
            code = cli.main(argv)
        return code, publish

    def test_posts_one_item_and_records_it(self):
        code, publish = self.run_cli(["post"])
        self.assertEqual(code, 0)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(State.load(self.state_path).posted_ids, {"a"})

    def test_second_run_moves_to_the_next_item(self):
        self.run_cli(["post"])
        code, publish = self.run_cli(["post"])
        self.assertEqual(code, 0)
        self.assertEqual(publish.call_args.args[1].id, "b")
        self.assertEqual(State.load(self.state_path).posted_ids, {"a", "b"})

    def test_nothing_to_post_is_not_an_error(self):
        State(path=self.state_path, posted={"a": {}, "b": {}}).save()
        code, publish = self.run_cli(["post"])
        self.assertEqual(code, 0)
        self.assertEqual(publish.call_count, 0)

    def test_limit_posts_multiple_items(self):
        code, publish = self.run_cli(["post", "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(publish.call_count, 2)

    def test_api_failure_exits_nonzero_and_records_nothing(self):
        code, _ = self.run_cli(["post"], publish_side_effect=ThreadsAPIError("失敗"))
        self.assertEqual(code, 1)
        self.assertEqual(State.load(self.state_path).posted_ids, set())

    def test_dry_run_needs_no_credentials_and_records_nothing(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("THREADS_USER")}
        env.pop("THREADS_ACCESS_TOKEN")
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "threads_bot.cli.publish_item", return_value=None
        ) as publish:
            code = cli.main(["post", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(publish.call_args.kwargs["dry_run"])
        self.assertFalse(self.state_path.exists())

    def test_validate_needs_no_credentials(self):
        env = {
            "THREADS_QUEUE_PATH": str(self.queue_path),
            "THREADS_STATE_PATH": str(self.state_path),
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cli.main(["validate"]), 0)

    def test_validate_rejects_a_pending_item_without_a_schedule(self):
        self.queue_path.write_text(
            json.dumps({"id": "c", "text": "日時なし"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", self.env, clear=True):
            self.assertEqual(cli.main(["validate"]), 1)

    def test_validate_allows_a_posted_item_without_a_schedule(self):
        self.queue_path.write_text(
            json.dumps({"id": "c", "text": "投稿済み"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        State(path=self.state_path, posted={"c": {}}).save()
        with mock.patch.dict("os.environ", self.env, clear=True):
            self.assertEqual(cli.main(["validate"]), 0)

    def test_validate_reports_a_broken_queue(self):
        self.queue_path.write_text("{壊れた\n", encoding="utf-8")
        with mock.patch.dict("os.environ", self.env, clear=True):
            self.assertEqual(cli.main(["validate"]), 1)

    def test_missing_credentials_exit_nonzero(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cli.main(["post"]), 1)


class StateTest(unittest.TestCase):
    def test_round_trips_through_disk(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "posted.json"
            state = State.load(path)
            state.record("a", post_id="p1", permalink="https://example.test/p1")
            state.save()
            reloaded = State.load(path)
        self.assertEqual(reloaded.posted_ids, {"a"})
        self.assertEqual(reloaded.posted["a"]["post_id"], "p1")
        self.assertIn("posted_at", reloaded.posted["a"])
