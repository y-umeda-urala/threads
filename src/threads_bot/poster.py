"""キューの項目を実際に Threads へ投稿する処理。"""

from __future__ import annotations

import logging

from .client import PostResult, ThreadsClient
from .config import Config
from .queue import QueueItem

logger = logging.getLogger(__name__)


def publish_item(
    client: ThreadsClient,
    item: QueueItem,
    config: Config,
    *,
    dry_run: bool = False,
) -> PostResult | None:
    """1 件（連投がある場合はその一連）を投稿する。dry-run なら何も送らない。"""
    if dry_run:
        logger.info(
            "[dry-run] 投稿しません id=%s media_type=%s text=%r thread=%d件",
            item.id,
            item.media_type,
            item.text,
            len(item.thread),
        )
        return None

    creation_id = client.create_container(
        text=item.text,
        media_type=item.media_type,
        image_url=item.image_url,
        video_url=item.video_url,
        link_attachment=item.link_attachment,
        reply_control=item.reply_control,
        alt_text=item.alt_text,
    )
    _wait_for_container(client, creation_id, item.media_type, config)
    post_id = client.publish(creation_id)
    logger.info("投稿しました id=%s post_id=%s", item.id, post_id)

    # 連投は 1 件目への返信として順につなげる
    reply_to = post_id
    for index, text in enumerate(item.thread, start=2):
        reply_container = client.create_container(text=text, reply_to_id=reply_to)
        _wait_for_container(client, reply_container, "TEXT", config)
        reply_to = client.publish(reply_container)
        logger.info("連投 %d 件目を投稿しました post_id=%s", index, reply_to)

    return PostResult(
        creation_id=creation_id, post_id=post_id, permalink=client.permalink(post_id)
    )


def _wait_for_container(
    client: ThreadsClient, container_id: str, media_type: str, config: Config
) -> None:
    if media_type == "TEXT":
        # テキストは即時に公開できるが、直後の publish が稀に失敗するため少し待つ
        client.sleep(config.publish_delay)
        return
    client.wait_until_ready(container_id, interval=config.media_publish_delay / 6)
