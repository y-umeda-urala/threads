import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from threads_bot.queue import QueueError, load_queue, select_due, without_schedule

JST = ZoneInfo("Asia/Tokyo")


def write_queue(directory: Path, rows: list) -> Path:
    path = directory / "queue.jsonl"
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class LoadQueueTest(unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), ["# コメント", "", {"text": "本文"}])
            items = load_queue(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "本文")

    def test_id_is_derived_from_text_when_missing(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "同じ本文"}])
            first = load_queue(path)[0]
            second = load_queue(path)[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(first.id), 12)

    def test_naive_scheduled_at_uses_configured_timezone(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "予約", "scheduled_at": "2026-09-01T09:00"}])
            item = load_queue(path, timezone="Asia/Tokyo")[0]
        self.assertEqual(item.scheduled_at, datetime(2026, 9, 1, 9, 0, tzinfo=JST))

    def test_media_type_follows_the_attached_media(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(
                Path(tmp),
                [
                    {"text": "文字だけ"},
                    {"text": "画像", "image_url": "https://example.com/a.jpg"},
                    {"text": "動画", "video_url": "https://example.com/a.mp4"},
                ],
            )
            items = load_queue(path)
        self.assertEqual([i.media_type for i in items], ["TEXT", "IMAGE", "VIDEO"])

    def test_duplicate_ids_are_rejected(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"id": "a", "text": "1"}, {"id": "a", "text": "2"}])
            with self.assertRaises(QueueError) as ctx:
                load_queue(path)
        self.assertIn("重複", str(ctx.exception))

    def test_too_long_text_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "あ" * 501}])
            with self.assertRaises(QueueError):
                load_queue(path)

    def test_empty_text_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "   "}])
            with self.assertRaises(QueueError):
                load_queue(path)

    def test_image_and_video_cannot_be_combined(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(
                Path(tmp),
                [{"text": "x", "image_url": "https://a/i.jpg", "video_url": "https://a/v.mp4"}],
            )
            with self.assertRaises(QueueError):
                load_queue(path)

    def test_unknown_reply_control_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "x", "reply_control": "nobody"}])
            with self.assertRaises(QueueError):
                load_queue(path)

    def test_broken_json_reports_the_line_number(self):
        with TemporaryDirectory() as tmp:
            path = write_queue(Path(tmp), [{"text": "ok"}, "{壊れた"])
            with self.assertRaises(QueueError) as ctx:
                load_queue(path)
        self.assertIn("2 行目", str(ctx.exception))

    def test_missing_file_reports_the_path(self):
        with self.assertRaises(QueueError):
            load_queue("/does/not/exist.jsonl")


class SelectDueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = write_queue(
            Path(self.tmp.name),
            [
                {"id": "plain-1", "text": "予約なし 1"},
                {"id": "future", "text": "未来", "scheduled_at": "2026-12-31T09:00"},
                {"id": "past", "text": "過去", "scheduled_at": "2026-01-01T09:00"},
                {"id": "plain-2", "text": "予約なし 2"},
            ],
        )
        self.items = load_queue(self.path)
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=JST)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_due_scheduled_items_are_selected(self):
        due = select_due(self.items, set(), now=self.now, limit=10)
        self.assertEqual([i.id for i in due], ["past"])

    def test_future_items_are_not_selected(self):
        due = select_due(self.items, set(), now=self.now, limit=10)
        self.assertNotIn("future", [i.id for i in due])

    def test_items_without_a_schedule_are_never_selected(self):
        due = select_due(self.items, set(), now=self.now, limit=10)
        self.assertNotIn("plain-1", [i.id for i in due])
        self.assertNotIn("plain-2", [i.id for i in due])

    def test_posted_items_are_skipped(self):
        due = select_due(self.items, {"past"}, now=self.now, limit=10)
        self.assertEqual(due, [])

    def test_due_items_come_out_in_time_order(self):
        earlier = datetime(2026, 5, 1, 9, 0, tzinfo=JST)
        self.items[1].scheduled_at = earlier  # future -> 過去にずらす
        due = select_due(self.items, set(), now=self.now, limit=10)
        self.assertEqual([i.id for i in due], ["past", "future"])

    def test_limit_caps_the_result(self):
        self.items[1].scheduled_at = datetime(2026, 5, 1, 9, 0, tzinfo=JST)
        self.assertEqual(len(select_due(self.items, set(), now=self.now, limit=1)), 1)
        self.assertEqual(select_due(self.items, set(), now=self.now, limit=0), [])

    def test_nothing_left_returns_empty(self):
        all_ids = {i.id for i in self.items}
        self.assertEqual(select_due(self.items, all_ids, now=self.now, limit=5), [])

    def test_without_schedule_finds_stranded_items(self):
        stranded = without_schedule(self.items, set())
        self.assertEqual([i.id for i in stranded], ["plain-1", "plain-2"])

    def test_without_schedule_ignores_posted_items(self):
        stranded = without_schedule(self.items, {"plain-1", "plain-2"})
        self.assertEqual(stranded, [])
