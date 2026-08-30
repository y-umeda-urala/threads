"""threads-bot コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import ThreadsAPIError, ThreadsClient, refresh_long_lived_token
from .config import Config, ConfigError
from .poster import publish_item
from .queue import QueueError, QueueItem, load_queue, select_due, without_schedule
from .state import State

logger = logging.getLogger("threads_bot")


def _build_client(config: Config) -> ThreadsClient:
    return ThreadsClient(
        config.user_id,
        config.access_token,
        api_base=config.api_base,
        timeout=config.timeout,
    )


def cmd_post(args: argparse.Namespace, config: Config) -> int:
    items = load_queue(config.queue_path, timezone=config.timezone)
    state = State.load(config.state_path)
    now = datetime.now(ZoneInfo(config.timezone))
    due = select_due(items, state.posted_ids, now=now, limit=args.limit)

    if not due:
        remaining = len([i for i in items if i.id not in state.posted_ids])
        logger.info("予約時刻が来た項目はありません（未投稿の残り %d 件）", remaining)
        return 0

    failures = 0
    for item in due:
        try:
            result = publish_item(_build_client(config), item, config, dry_run=args.dry_run)
        except ThreadsAPIError as exc:
            failures += 1
            logger.error("投稿に失敗しました id=%s: %s", item.id, exc)
            continue
        if result is not None:
            state.record(item.id, post_id=result.post_id, permalink=result.permalink)
            state.save()
            if result.permalink:
                logger.info("URL: %s", result.permalink)

    return 1 if failures else 0


def cmd_validate(args: argparse.Namespace, config: Config) -> int:
    items = load_queue(config.queue_path, timezone=config.timezone)
    state = State.load(config.state_path)
    pending = [i for i in items if i.id not in state.posted_ids]

    stranded = without_schedule(items, state.posted_ids)
    if stranded:
        for item in stranded:
            logger.error(
                "%d 行目 [%s]: scheduled_at がありません。このままでは投稿されません。",
                item.line_number,
                item.id,
            )
        return 1

    logger.info(
        "キュー %s: 全 %d 件 / 未投稿 %d 件 — 形式に問題はありません",
        config.queue_path,
        len(items),
        len(pending),
    )
    for item in pending[: args.limit]:
        logger.info("  次: %s", _describe(item))
    return 0


def _describe(item: QueueItem) -> str:
    when = item.scheduled_at.isoformat() if item.scheduled_at else "日時なし"
    head = item.text.replace("\n", " ")[:40]
    return f"[{item.id}] {when} {item.media_type} {head!r}"


def cmd_me(_args: argparse.Namespace, config: Config) -> int:
    data = ThreadsClient("me", config.access_token, api_base=config.api_base).me()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    logger.info("この id を Secret THREADS_USER_ID に設定してください: %s", data.get("id"))
    return 0


def cmd_limit(_args: argparse.Namespace, config: Config) -> int:
    data = _build_client(config).publishing_limit()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_refresh_token(_args: argparse.Namespace, config: Config) -> int:
    data = refresh_long_lived_token(
        config.access_token, api_base=config.api_base, timeout=config.timeout
    )
    expires_in = int(data.get("expires_in", 0))
    logger.info("トークンを更新しました（残り %d 日）", expires_in // 86400)
    # 呼び出し側（GitHub Actions）が Secret に書き戻せるよう標準出力へ JSON を出す
    print(json.dumps(data, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threads-bot", description="Threads への自動投稿ボット"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="デバッグログを出す")
    sub = parser.add_subparsers(dest="command", required=True)

    post = sub.add_parser("post", help="キューから投稿する")
    post.add_argument("--limit", type=int, default=1, help="1 回の実行で投稿する件数（既定 1）")
    post.add_argument("--dry-run", action="store_true", help="API を呼ばずに内容だけ表示する")
    post.set_defaults(func=cmd_post)

    validate = sub.add_parser("validate", help="キューの形式を検証する")
    validate.add_argument("--limit", type=int, default=5, help="表示する未投稿件数")
    validate.set_defaults(func=cmd_validate, needs_credentials=False)

    me = sub.add_parser("me", help="トークンの持ち主と user_id を確認する")
    me.set_defaults(func=cmd_me, needs_user_id=False)

    limit = sub.add_parser("limit", help="24 時間あたりの投稿枠の使用状況を見る")
    limit.set_defaults(func=cmd_limit)

    refresh = sub.add_parser("refresh-token", help="長期アクセストークンを更新する")
    refresh.set_defaults(func=cmd_refresh_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        needs_credentials = getattr(args, "needs_credentials", True) and not getattr(
            args, "dry_run", False
        )
        config = Config.from_env(
            require_credentials=needs_credentials,
            require_user_id=getattr(args, "needs_user_id", True),
        )
        return args.func(args, config)
    except (ConfigError, QueueError, ThreadsAPIError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
