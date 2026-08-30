"""Threads Graph API の薄いクライアント（標準ライブラリのみ）。

投稿は 2 段階:
  1. POST /{user-id}/threads          → メディアコンテナを作る (creation_id が返る)
  2. POST /{user-id}/threads_publish  → creation_id を指定して公開する
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Threads のテキスト上限（1 投稿あたり 500 文字）
MAX_TEXT_LENGTH = 500
# 一時的な失敗とみなして再試行する HTTP ステータス
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ThreadsAPIError(Exception):
    """Threads API がエラーを返した、または通信に失敗した。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode
        self.retryable = retryable


@dataclass(frozen=True)
class PostResult:
    creation_id: str
    post_id: str
    permalink: str | None = None


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """テストから差し替えやすいように urlopen を 1 箇所に閉じ込める。"""
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


class ThreadsClient:
    def __init__(
        self,
        user_id: str,
        access_token: str,
        *,
        api_base: str = "https://graph.threads.net/v1.0",
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Any = time.sleep,
        opener: Any = _urlopen,
    ) -> None:
        self.user_id = user_id
        self.access_token = access_token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self._opener = opener

    # ------------------------------------------------------------------ HTTP

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        payload["access_token"] = self.access_token
        url = f"{self.api_base}/{path.lstrip('/')}"
        encoded = urllib.parse.urlencode(payload).encode()

        if method == "GET":
            request = urllib.request.Request(f"{url}?{encoded.decode()}", method="GET")
        else:
            request = urllib.request.Request(url, data=encoded, method=method)

        last_error: ThreadsAPIError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                _status, body = self._opener(request, self.timeout)
                return json.loads(body.decode() or "{}")
            except urllib.error.HTTPError as exc:  # 4xx / 5xx
                last_error = _http_error_to_api_error(exc)
            except urllib.error.URLError as exc:  # DNS・接続断など
                last_error = ThreadsAPIError(f"通信に失敗しました: {exc.reason}", retryable=True)
            except json.JSONDecodeError as exc:
                last_error = ThreadsAPIError(f"レスポンスの JSON 解析に失敗しました: {exc}")

            if not last_error.retryable or attempt == self.max_retries:
                raise last_error
            backoff = 2.0**attempt
            logger.warning(
                "%s %s が失敗 (%s)。%.0f 秒後に再試行します (%d/%d)",
                method,
                path,
                last_error,
                backoff,
                attempt,
                self.max_retries,
            )
            self.sleep(backoff)

        raise last_error  # pragma: no cover - ループ内で必ず raise される

    # --------------------------------------------------------------- 投稿 API

    def create_container(
        self,
        *,
        text: str | None = None,
        media_type: str = "TEXT",
        image_url: str | None = None,
        video_url: str | None = None,
        reply_to_id: str | None = None,
        reply_control: str | None = None,
        link_attachment: str | None = None,
        alt_text: str | None = None,
    ) -> str:
        """メディアコンテナを作成し、creation_id を返す。"""
        if text is not None and len(text) > MAX_TEXT_LENGTH:
            raise ThreadsAPIError(
                f"本文が {MAX_TEXT_LENGTH} 文字を超えています ({len(text)} 文字)"
            )
        params = {
            "media_type": media_type,
            "text": text,
            "image_url": image_url,
            "video_url": video_url,
            "reply_to_id": reply_to_id,
            "reply_control": reply_control,
            "alt_text": alt_text,
        }
        # link_attachment はテキスト投稿でのみ有効
        if media_type == "TEXT":
            params["link_attachment"] = link_attachment
        response = self._request("POST", f"{self.user_id}/threads", params)
        container_id = response.get("id")
        if not container_id:
            raise ThreadsAPIError(f"コンテナ ID が返りませんでした: {response}")
        return str(container_id)

    def publish(self, creation_id: str) -> str:
        """コンテナを公開し、投稿 ID を返す。"""
        response = self._request(
            "POST", f"{self.user_id}/threads_publish", {"creation_id": creation_id}
        )
        post_id = response.get("id")
        if not post_id:
            raise ThreadsAPIError(f"投稿 ID が返りませんでした: {response}")
        return str(post_id)

    def container_status(self, container_id: str) -> dict[str, Any]:
        return self._request(
            "GET", container_id, {"fields": "status,error_message"}
        )

    def wait_until_ready(
        self, container_id: str, *, attempts: int = 10, interval: float = 5.0
    ) -> None:
        """コンテナが FINISHED になるまで待つ（画像・動画向け）。"""
        for _ in range(attempts):
            status = self.container_status(container_id)
            state = status.get("status")
            if state == "FINISHED":
                return
            if state == "ERROR":
                raise ThreadsAPIError(
                    f"コンテナの処理に失敗しました: {status.get('error_message')}"
                )
            self.sleep(interval)
        raise ThreadsAPIError(
            f"コンテナ {container_id} が時間内に FINISHED になりませんでした"
        )

    def permalink(self, post_id: str) -> str | None:
        try:
            return self._request("GET", post_id, {"fields": "permalink"}).get("permalink")
        except ThreadsAPIError as exc:  # パーマリンク取得の失敗で投稿を失敗扱いにしない
            logger.warning("パーマリンクの取得に失敗しました: %s", exc)
            return None

    def me(self) -> dict[str, Any]:
        """トークンの持ち主。user_id を調べるのに使う。"""
        return self._request(
            "GET", "me", {"fields": "id,username,threads_profile_picture_url"}
        )

    def publishing_limit(self) -> dict[str, Any]:
        """24 時間あたりの投稿枠の使用状況（上限 250 件）。"""
        return self._request(
            "GET",
            f"{self.user_id}/threads_publishing_limit",
            {"fields": "quota_usage,config,reply_quota_usage,reply_config"},
        )


def _http_error_to_api_error(exc: urllib.error.HTTPError) -> ThreadsAPIError:
    raw = exc.read().decode(errors="replace")
    message, code, subcode = raw, None, None
    try:
        error = json.loads(raw).get("error", {})
        message = error.get("message", raw)
        code = error.get("code")
        subcode = error.get("error_subcode")
    except (json.JSONDecodeError, AttributeError):
        pass
    return ThreadsAPIError(
        f"Threads API エラー (HTTP {exc.code}): {message}",
        status=exc.code,
        code=code,
        subcode=subcode,
        retryable=exc.code in RETRYABLE_STATUS,
    )


def refresh_long_lived_token(
    access_token: str,
    *,
    api_base: str = "https://graph.threads.net/v1.0",
    timeout: float = 30.0,
    opener: Any = _urlopen,
) -> dict[str, Any]:
    """長期トークン（有効期限 60 日）を更新する。

    更新できるのは発行から 24 時間以上経過した長期トークンのみ。
    """
    root = api_base.rstrip("/").rsplit("/", 1)[0] if api_base.endswith("v1.0") else api_base
    query = urllib.parse.urlencode(
        {"grant_type": "th_refresh_token", "access_token": access_token}
    )
    request = urllib.request.Request(f"{root}/refresh_access_token?{query}", method="GET")
    try:
        _status, body = opener(request, timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error_to_api_error(exc) from exc
    except urllib.error.URLError as exc:
        raise ThreadsAPIError(f"通信に失敗しました: {exc.reason}") from exc
    return json.loads(body.decode() or "{}")
