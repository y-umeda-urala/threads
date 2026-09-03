"""翌日ぶんの Threads 投稿 10 本を作成し、投稿キューに追加する。

外部 cron から毎日きまった時刻（例: 22:00 JST）に起動される想定。

材料:
  - 運用ボード（Google ドキュメント / リンクを知っている全員が閲覧可）
  - ネタ帳（同上）
  - URALA サイトの新着記事（https://urala.today/feed/ の RSS）
  - posts/queue.jsonl の直近の投稿（重複回避のため）

必要な環境変数:
  ANTHROPIC_API_KEY  必須。Anthropic の API キー
  BOARD_DOC_ID       任意。運用ボードの Google ドキュメント ID
  NETA_DOC_ID        任意。ネタ帳の Google ドキュメント ID
  ANTHROPIC_MODEL    任意。使うモデル。未指定なら利用可能なものから自動で選ぶ
  DRY_RUN            任意。"true" なら生成結果を表示するだけでファイルを書き換えない
"""

from __future__ import annotations

import json
import os
import re
import random
import string
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
QUEUE_PATH = Path("posts/queue.jsonl")
API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
URALA_FEED_URL = "https://urala.today/feed/"

# 予約する時刻と、その枠の役割。半分は URALA の新着記事紹介、半分は人物寄りの投稿。
SLOTS = [
    (5, "URALA新着記事紹介",
