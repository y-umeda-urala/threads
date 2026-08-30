"""環境変数から読み込む実行時設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_API_BASE = "https://graph.threads.net/v1.0"
DEFAULT_TIMEZONE = "Asia/Tokyo"


class ConfigError(Exception):
    """必須の設定が欠けている、または値が不正。"""


@dataclass(frozen=True)
class Config:
    user_id: str
    access_token: str
    api_base: str = DEFAULT_API_BASE
    timezone: str = DEFAULT_TIMEZONE
    queue_path: str = "posts/queue.jsonl"
    state_path: str = "state/posted.json"
    timeout: float = 30.0
    # コンテナ作成から publish までの待機秒数。テキストのみなら短くてよいが、
    # 画像・動画はサーバー側の処理完了を待つ必要がある。
    publish_delay: float = 5.0
    media_publish_delay: float = 30.0

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        require_credentials: bool = True,
        require_user_id: bool = True,
    ) -> "Config":
        env = dict(os.environ if env is None else env)
        user_id = env.get("THREADS_USER_ID", "").strip()
        access_token = env.get("THREADS_ACCESS_TOKEN", "").strip()
        required = [("THREADS_ACCESS_TOKEN", access_token)]
        if require_user_id:
            required.insert(0, ("THREADS_USER_ID", user_id))
        missing = [name for name, value in required if not value]
        if missing and require_credentials:
            raise ConfigError(
                "必須の環境変数が設定されていません: " + ", ".join(missing)
            )
        return cls(
            user_id=user_id,
            access_token=access_token,
            api_base=env.get("THREADS_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            timezone=env.get("THREADS_TIMEZONE", DEFAULT_TIMEZONE),
            queue_path=env.get("THREADS_QUEUE_PATH", "posts/queue.jsonl"),
            state_path=env.get("THREADS_STATE_PATH", "state/posted.json"),
            timeout=float(env.get("THREADS_TIMEOUT", "30")),
            publish_delay=float(env.get("THREADS_PUBLISH_DELAY", "5")),
            media_publish_delay=float(env.get("THREADS_MEDIA_PUBLISH_DELAY", "30")),
        )
