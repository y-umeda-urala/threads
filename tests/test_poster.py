import unittest

from threads_bot.config import Config
from threads_bot.poster import publish_item
from threads_bot.queue import QueueItem


class RecordingClient:
    def __init__(self):
        self.containers = []
        self.published = []
        self.sleep = lambda _s: None
        self._next = 0

    def create_container(self, **kwargs):
        self._next += 1
        self.containers.append(kwargs)
        return f"c{self._next}"

    def publish(self, creation_id):
        self.published.append(creation_id)
        return creation_id.replace("c", "p")

    def wait_until_ready(self, container_id, **kwargs):
        self.waited = container_id

    def permalink(self, post_id):
        return f"https://www.threads.net/t/{post_id}"


CONFIG = Config(user_id="1", access_token="t", publish_delay=0, media_publish_delay=0)


class PublishItemTest(unittest.TestCase):
    def test_publishes_a_text_post(self):
        client = RecordingClient()
        result = publish_item(client, QueueItem(id="a", text="本文"), CONFIG)
        self.assertEqual(result.post_id, "p1")
        self.assertEqual(client.published, ["c1"])
        self.assertEqual(client.containers[0]["text"], "本文")
        self.assertTrue(result.permalink.endswith("p1"))

    def test_thread_parts_reply_to_the_previous_post(self):
        client = RecordingClient()
        item = QueueItem(id="a", text="1件目", thread=["2件目", "3件目"])
        publish_item(client, item, CONFIG)
        self.assertEqual(client.published, ["c1", "c2", "c3"])
        self.assertEqual(client.containers[1]["reply_to_id"], "p1")
        self.assertEqual(client.containers[2]["reply_to_id"], "p2")

    def test_media_posts_wait_for_the_container(self):
        client = RecordingClient()
        item = QueueItem(id="a", text="写真", image_url="https://a/i.jpg")
        publish_item(client, item, CONFIG)
        self.assertEqual(client.waited, "c1")
        self.assertEqual(client.containers[0]["media_type"], "IMAGE")

    def test_dry_run_sends_nothing(self):
        client = RecordingClient()
        result = publish_item(client, QueueItem(id="a", text="本文"), CONFIG, dry_run=True)
        self.assertIsNone(result)
        self.assertEqual(client.containers, [])
        self.assertEqual(client.published, [])
