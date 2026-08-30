import json
import unittest
import urllib.error
import urllib.parse
from io import BytesIO

from threads_bot.client import ThreadsAPIError, ThreadsClient


class FakeOpener:
    """urlopen の代わり。呼ばれたリクエストを記録し、用意した応答を順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        body = request.data.decode() if request.data else urllib.parse.urlparse(request.full_url).query
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url.split("?")[0],
                "params": dict(urllib.parse.parse_qsl(body)),
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return 200, json.dumps(response).encode()


def http_error(status, payload):
    return urllib.error.HTTPError(
        "https://example.test", status, "err", {}, BytesIO(json.dumps(payload).encode())
    )


def make_client(responses, **kwargs):
    opener = FakeOpener(responses)
    client = ThreadsClient(
        "123", "token", opener=opener, sleep=lambda _s: None, api_base="https://api.test/v1.0", **kwargs
    )
    return client, opener


class CreateContainerTest(unittest.TestCase):
    def test_sends_text_and_returns_creation_id(self):
        client, opener = make_client([{"id": "c1"}])
        self.assertEqual(client.create_container(text="こんにちは"), "c1")
        call = opener.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.test/v1.0/123/threads")
        self.assertEqual(call["params"]["text"], "こんにちは")
        self.assertEqual(call["params"]["media_type"], "TEXT")
        self.assertEqual(call["params"]["access_token"], "token")

    def test_omits_unset_optional_params(self):
        client, opener = make_client([{"id": "c1"}])
        client.create_container(text="x")
        self.assertNotIn("image_url", opener.calls[0]["params"])
        self.assertNotIn("reply_to_id", opener.calls[0]["params"])

    def test_link_attachment_is_dropped_for_media_posts(self):
        client, opener = make_client([{"id": "c1"}])
        client.create_container(
            text="x", media_type="IMAGE", image_url="https://a/i.jpg", link_attachment="https://a"
        )
        self.assertNotIn("link_attachment", opener.calls[0]["params"])

    def test_rejects_text_over_the_limit(self):
        client, _ = make_client([{"id": "c1"}])
        with self.assertRaises(ThreadsAPIError):
            client.create_container(text="あ" * 501)

    def test_missing_id_in_response_is_an_error(self):
        client, _ = make_client([{}])
        with self.assertRaises(ThreadsAPIError):
            client.create_container(text="x")


class PublishTest(unittest.TestCase):
    def test_publishes_with_creation_id(self):
        client, opener = make_client([{"id": "p1"}])
        self.assertEqual(client.publish("c1"), "p1")
        self.assertEqual(opener.calls[0]["url"], "https://api.test/v1.0/123/threads_publish")
        self.assertEqual(opener.calls[0]["params"]["creation_id"], "c1")


class RetryTest(unittest.TestCase):
    def test_retries_on_server_error_then_succeeds(self):
        client, opener = make_client([http_error(503, {"error": {"message": "busy"}}), {"id": "c1"}])
        self.assertEqual(client.create_container(text="x"), "c1")
        self.assertEqual(len(opener.calls), 2)

    def test_does_not_retry_client_errors(self):
        client, opener = make_client(
            [http_error(400, {"error": {"message": "bad", "code": 100}})] * 3
        )
        with self.assertRaises(ThreadsAPIError) as ctx:
            client.create_container(text="x")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(ctx.exception.code, 100)
        self.assertIn("bad", str(ctx.exception))

    def test_gives_up_after_max_retries(self):
        client, opener = make_client([http_error(500, {"error": {"message": "x"}})] * 3)
        with self.assertRaises(ThreadsAPIError):
            client.create_container(text="x")
        self.assertEqual(len(opener.calls), 3)

    def test_rate_limit_is_retryable(self):
        client, _ = make_client([http_error(429, {"error": {"message": "limit"}}), {"id": "c1"}])
        self.assertEqual(client.create_container(text="x"), "c1")


class ContainerStatusTest(unittest.TestCase):
    def test_wait_until_ready_returns_when_finished(self):
        client, _ = make_client([{"status": "IN_PROGRESS"}, {"status": "FINISHED"}])
        client.wait_until_ready("c1", attempts=3, interval=0)

    def test_wait_until_ready_raises_on_error_status(self):
        client, _ = make_client([{"status": "ERROR", "error_message": "画像が読めません"}])
        with self.assertRaises(ThreadsAPIError) as ctx:
            client.wait_until_ready("c1", attempts=3, interval=0)
        self.assertIn("画像が読めません", str(ctx.exception))

    def test_wait_until_ready_times_out(self):
        client, _ = make_client([{"status": "IN_PROGRESS"}] * 2)
        with self.assertRaises(ThreadsAPIError):
            client.wait_until_ready("c1", attempts=2, interval=0)


class PermalinkTest(unittest.TestCase):
    def test_returns_none_when_lookup_fails(self):
        client, _ = make_client([http_error(400, {"error": {"message": "no"}})])
        self.assertIsNone(client.permalink("p1"))
