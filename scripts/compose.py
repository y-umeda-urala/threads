"""翌日ぶんの Threads 投稿 10 本を作成し、投稿キューに追加する。

外部 cron から毎日 20:00 JST に起動される想定。

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
URALA_DESIGN_URL = "https://urala-design.jp/"

SLOTS = [
    (6, "URALA新着記事紹介", "F（紹介型）", "朝いちばんに読んで、今日のURALAの話題を知れる"),
    (8, "制作の裏側／福井というローカル", "C（裏側型）またはA（気づき型）", "何をしている人かが自然に伝わる"),
    (10, "URALA新着記事紹介", "F（紹介型）", "通勤・通学時間に読まれる想定"),
    (12, "ノウハウ／仕事観", "B（ノウハウ型）またはA（気づき型）", "仕事の合間に読んで学びになる"),
    (14, "URALA新着記事紹介", "F（紹介型）", "お昼過ぎに読まれる想定"),
    (15, "お役立ち・問いかけ", "E（問いかけ型）またはB（ノウハウ型）", "休憩時間に軽く読める、仕事に関する気づき"),
    (17, "URALA新着記事紹介", "F（紹介型）", "仕事終わりに読まれる想定"),
    (19, "制作の裏側／福井というローカル", "C（裏側型）またはA（気づき型）", "夕方以降にゆっくり読まれる"),
    (21, "URALA新着記事紹介", "F（紹介型）", "夜、ゆっくりした時間に読まれる想定"),
    (23, "お役立ち・問いかけ", "E（問いかけ型）またはB（ノウハウ型）", "一日の終わりに読まれる、仕事に関する気づき"),
]

MODEL_PREFERENCE = ("opus", "sonnet", "haiku")


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def fetch_doc(doc_id: str, label: str) -> str:
    if not doc_id:
        print(f"{label}: ID が未設定のため読み込みません。")
        return ""
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.status != 200:
                print(f"::warning::{label}: 取得できませんでした (HTTP {response.status})")
                return ""
            text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"::warning::{label}: 取得に失敗しました ({exc})")
        return ""
    print(f"{label}: {len(text)} 文字を読み込みました。")
    return text


def fetch_urala_articles(limit: int = 10) -> str:
    try:
        request = urllib.request.Request(
            URALA_FEED_URL, headers={"User-Agent": "threads-bot/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                print(f"::warning::URALA新着記事: 取得できませんでした (HTTP {response.status})")
                return ""
            raw = response.read()
    except Exception as exc:
        print(f"::warning::URALA新着記事: 取得に失敗しました ({exc})")
        return ""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"::warning::URALA新着記事: RSS の解析に失敗しました ({exc})")
        return ""
    items = root.findall("./channel/item")[:limit]
    if not items:
        print("URALA新着記事: 0 件でした。")
        return ""
    lines = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        link = link.split("?")[0]
        description = (item.findtext("description") or "").strip()
        description = re.sub(r"<[^>]+>", "", description)
        description = re.sub(r"\s+", " ", description).strip()[:200]
        if not title or not link:
            continue
        lines.append(f"- 「{title}」 {link}\n  {description}")
    print(f"URALA新着記事: {len(lines)} 件を読み込みました。")
    return "\n".join(lines)

def fetch_urala_design_works(limit: int = 15) -> str:
    """ウララコミュニケーションズの制作実績（urala-design.jp）を取得し、材料として整形する。

    RSS が無いサイトなので、トップページの HTML から
    「/works/?cat_num=」を含むリンクのタイトルを正規表現で拾う簡易スクレイピング。
    サイト構造が変わって 0 件になっても、処理は止めず材料なしで続ける。
    """
    try:
        request = urllib.request.Request(
            URALA_DESIGN_URL, headers={"User-Agent": "threads-bot/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                print(f"::warning::URALA制作実績: 取得できませんでした (HTTP {response.status})")
                return ""
            html = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::URALA制作実績: 取得に失敗しました ({exc})")
        return ""

    pattern = re.compile(
        r'href="(https://urala-design\.jp/service/[a-z]+/works/\?cat_num=[a-z0-9]+)"[^>]*>\s*(.*?)\s*</a>',
        re.S,
    )
    seen = set()
    lines = []
    for url, raw_title in pattern.findall(html):
        title = re.sub(r"<[^>]+>", "", raw_title)
        title = re.sub(r"\s+", " ", title).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        lines.append(f"- 「{title}」 {url}")
        if len(lines) >= limit:
            break

    print(f"URALA制作実績: {len(lines)} 件を読み込みました。")
    return "\n".join(lines)

def api_request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(API_BASE + path, data=data, method=method)
    request.add_header("x-api-key", api_key)
    request.add_header("anthropic-version", API_VERSION)
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        fail(f"Anthropic API エラー ({exc.code}): {detail}")
    except Exception as exc:
        fail(f"Anthropic API に接続できませんでした: {exc}")
    return {}


def pick_model(api_key: str) -> str:
    explicit = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if explicit:
        return explicit
    payload = api_request("GET", "/models?limit=100", api_key)
    ids = [m["id"] for m in payload.get("data", [])]
    if not ids:
        fail("利用できるモデルが見つかりませんでした。ANTHROPIC_MODEL を指定してください。")
    for keyword in MODEL_PREFERENCE:
        for model_id in ids:
            if keyword in model_id:
                print(f"モデル: {model_id}")
                return model_id
    print(f"モデル: {ids[0]}")
    return ids[0]


def read_queue_lines() -> list[str]:
    if not QUEUE_PATH.exists():
        fail(f"キューが見つかりません: {QUEUE_PATH}")
    return QUEUE_PATH.read_text(encoding="utf-8").splitlines()


def parse_entries(lines: list[str]) -> list[dict]:
    entries = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return entries


def recent_texts(entries: list[dict], count: int = 20) -> str:
    parts = []
    for entry in entries[-count:]:
        text = entry.get("text", "")
        thread = " ".join(entry.get("thread") or [])
        parts.append(f"- {text} {thread}".strip())
    return "\n".join(parts)


def find_filled(entries: list[dict], target_date) -> dict[int, dict]:
    prefix = target_date.isoformat()
    filled: dict[int, dict] = {}
    for entry in entries:
        scheduled = entry.get("scheduled_at")
        if not isinstance(scheduled, str) or not scheduled.startswith(prefix):
            continue
        try:
            hour = int(scheduled[11:13])
        except (ValueError, IndexError):
            continue
        filled[hour] = entry
    return filled


def describe_filled(filled: dict[int, dict]) -> str:
    if not filled:
        return ""
    parts = []
    for hour in sorted(filled):
        entry = filled[hour]
        thread = " ".join(entry.get("thread") or [])
        parts.append(f"- {hour}:00 ｜ {entry.get('text','')} {thread}".strip())
    return "\n".join(parts)


def build_prompt(board: str, neta: str, articles: str, works: str, recent: str, target_date, needed, filled) -> str:
    slot_lines = "\n".join(
        f"- {hour}:00 ｜ 柱: {pillar} ｜ 型: {form} ｜ ねらい: {aim}"
        for hour, pillar, form, aim in needed
    )
    weekday = "月火水木金土日"[target_date.weekday()]
    hours = "、".join(f"{hour}:00" for hour, *_ in needed)
    hour_choices = "／".join(str(hour) for hour, *_ in SLOTS)
    already = describe_filled(filled)
    sections = [
        "あなたは 株式会社ウララコミュニケーションズ（福井のローカルメディア「日々URALA」運営・SNS運用／Web広告運用／ホームページ制作を担当）の",
        "Threads 発信チームの編集担当です。",
        f"{target_date.isoformat()}（{weekday}）の {hours} に投稿する {len(needed)} 本を書いてください。",
        "",
        "## 枠と役割",
        slot_lines,
        "",
        "柱が「URALA新着記事紹介」の枠は、下の「URALAサイトの新着記事」または",
        "「ウララコミュニケーションズの制作実績」のどちらか1件を選び、",
        "その記事・実績を紹介する投稿にしてください。URLは省略せずそのまま本文またはTHREADの末尾に書くこと",
        "（この枠に限り、下の文体ルールの「リンクは貼らない」を適用しません）。",
        "紹介する記事がない場合や新着記事の材料が空の場合は、無理に作らず柱を",
        "「お役立ち・問いかけ」に読み替えて書いてください。",
        "",
    ]
    if already:
        sections += [
            "## 同じ日にすでに入っている投稿（担当者本人が用意したもの）",
            "これらとネタ・切り口・書き出しが重ならないようにしてください。",
            "文体もこれらに寄せてください。",
            already,
            "",
        ]
    sections += [
        "## 運用ボード（最優先のルール。以下の指示と食い違ったらボードを優先する）",
        board or "（読み込めませんでした。以下の要点だけで書いてください）",
        "",
        "## ネタ帳（担当者本人が書いた生の材料。最優先で使う）",
        neta or "（空です）",
        "",
        "## URALAサイトの新着記事（記事紹介枠の材料。タイトルと概要の範囲で紹介し、内容を創作しない）",
        articles or "（取得できませんでした）",
        "",
        "## ウララコミュニケーションズの制作実績（urala-design.jp。記事紹介枠でこちらを紹介してもよい）",
        works or "（取得できませんでした）",
        "",
        "## 直近の投稿（ネタ・切り口・書き出しの重複を避けるため）",
        recent or "（なし）",
        "",
        "## 文体の要点",
        "- 丁寧で落ち着いた敬語。です・ます調",
        "- 一文は短く。3〜4 行ごとに空行",
        "- 冒頭 1 行で引き込む",
        "- 絵文字は使わない。ハッシュタグは 0〜1 個",
        "- リンクは貼らない（ただし URALA新着記事紹介の枠は除く。その枠は記事URLを書くこと）",
        "- 1 投稿につき伝えたいことは 1 つだけ",
        "- クライアント実名は出さない（「福井の解体業の会社さん」のように業種で表現する）",
        "- 金額・社内事情・未公開情報は書かない",
        "- 誇張しない、盛らない。自慢に読めないよう、学び・失敗・裏側の形で語る",
        "",
        "## 事実について（最重要）",
        "確認できない事実を創作しないこと。ネタ帳・運用ボード・新着記事・直近の投稿に根拠がある内容だけを書く。",
        "成果や反響（「問い合わせが増えました」など）は、根拠がない限り絶対に書かない。",
        "材料が足りなければ、材料のある範囲で小さく書く。",
        "ネタ帳の「使ってほしくないネタ」に書かれた話題は絶対に使わない。",
        "",
        "## 長さと形",
        "- text は 40〜200 字。続きは thread に回す",
        "- thread は 1〜2 件。1 件あたり 500 字以内",
        "- text も thread も 500 字を超えないこと",
        "",
        "## 出力形式",
        "JSON では返さないでください。次の形式のテキストだけを返します。",
        "前後に説明や ``` を付けないこと。",
        "",
        "@@@POST",
        "HOUR: 6",
        "NOTE: 使った柱と型とネタ",
        "TEXT:",
        "本文をここに書く。改行や空行はそのまま書いてよい。",
        "THREAD:",
        "連投の 1 件目。改行や空行はそのまま書いてよい。",
        "THREAD:",
        "連投の 2 件目。無ければこの 2 行ごと省く。",
        "@@@END",
        "",
        f"{hours} のぶんを、この順に @@@POST 〜 @@@END の組で並べてください。",
        f"HOUR には {hour_choices} のいずれかの数字だけを書きます。",
    ]
    return "\n".join(sections)


def parse_posts(text: str) -> list[dict]:
    posts = []
    for body in re.findall(r"@@@POST[ \t]*\n(.*?)\n?@@@END", text, re.S):
        item = {"hour": None, "note": "", "text": "", "thread": []}
        tokens = re.split(r"^(HOUR:|NOTE:|TEXT:|THREAD:)", body, flags=re.M)
        for key, value in zip(tokens[1::2], tokens[2::2]):
            value = value.strip()
            if key == "HOUR:":
                digits = re.sub(r"\D", "", value)
                item["hour"] = int(digits) if digits else None
            elif key == "NOTE:":
                item["note"] = value
            elif key == "TEXT:":
                item["text"] = value
            elif key == "THREAD:" and value:
                item["thread"].append(value)
        if item["hour"] is not None and item["text"]:
            posts.append(item)
    return posts


def ask(api_key: str, model: str, prompt: str) -> str:
    payload = api_request(
        "POST",
        "/messages",
        api_key,
        {
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    return "".join(
        block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
    ).strip()


def generate(api_key: str, model: str, prompt: str, expected: int) -> list[dict]:
    reminder = (
        "\n\n---\n直前の返答は形式が守られていませんでした。"
        "説明や ``` を付けず、@@@POST 〜 @@@END の組だけを返してください。"
    )
    for attempt in (1, 2):
        text = ask(api_key, model, prompt if attempt == 1 else prompt + reminder)
        posts = parse_posts(text)
        if len(posts) == expected:
            return posts
        print(f"::warning::{attempt} 回目: {expected} 本のはずが {len(posts)} 本でした。")
        if attempt == 2:
            fail(
                f"{expected} 本を作れませんでした（2 回試行）。\n--- 生の出力 ---\n{text[:1200]}"
            )
    return []


def new_id(hour: int, existing: set[str]) -> str:
    stamp = datetime.now(JST).strftime("%Y%m%d")
    while True:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        candidate = f"p-{stamp}{hour:02d}-{suffix}"
        if candidate not in existing:
            return candidate


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        fail("ANTHROPIC_API_KEY が未設定です。リポジトリの Secrets に登録してください。")

    dry_run = os.environ.get("DRY_RUN", "").lower() == "true"
    target_date = (datetime.now(JST) + timedelta(days=1)).date()
    print(f"作成対象: {target_date}（日本時間）")

    lines = read_queue_lines()
    entries = parse_entries(lines)
    existing_ids = {str(e.get("id")) for e in entries if e.get("id")}

    filled = find_filled(entries, target_date)
    needed = [slot for slot in SLOTS if slot[0] not in filled]
    if filled:
        print("すでに予約済みの枠: " + "、".join(f"{h}:00" for h in sorted(filled)))
    if not needed:
        print(f"{target_date} は {len(SLOTS)} 枠とも埋まっています。何もしません。")
        return
    print("これから作る枠: " + "、".join(f"{h}:00" for h, *_ in needed))

    board = fetch_doc(os.environ.get("BOARD_DOC_ID", "").strip(), "運用ボード")
    neta = fetch_doc(os.environ.get("NETA_DOC_ID", "").strip(), "ネタ帳")
    articles = fetch_urala_articles()
    works = fetch_urala_design_works()

    model = pick_model(api_key)
    prompt = build_prompt(board, neta, articles, works, recent_texts(entries), target_date, needed, filled)
    posts = generate(api_key, model, prompt, len(needed))

    by_hour = {int(p["hour"]): p for p in posts}
    new_lines = []
    for hour, *_ in needed:
        post = by_hour.get(hour)
        if not post:
            fail(f"{hour}:00 の投稿が返ってきませんでした。")
        text = (post.get("text") or "").strip()
        if not text:
            fail(f"{hour}:00 の本文が空です。")
        thread = [t.strip() for t in (post.get("thread") or []) if t and t.strip()]
        for part in [text, *thread]:
            if len(part) > 500:
                fail(f"{hour}:00 に 500 字を超える要素があります（{len(part)} 字）。")
        item = {
            "id": new_id(hour, existing_ids),
            "text": text,
            "scheduled_at": f"{target_date.isoformat()}T{hour:02d}:00:00+09:00",
        }
        existing_ids.add(item["id"])
        if thread:
            item["thread"] = thread
        new_lines.append(json.dumps(item, ensure_ascii=False))
        print(f"\n=== {hour}:00 ({len(text)} 字) ===\n{text}")
        for index, part in enumerate(thread, start=2):
            print(f"--- 連投 {index} ({len(part)} 字) ---\n{part}")
        if post.get("note"):
            print(f"[メモ] {post['note']}")

    if dry_run:
        print("\nDRY_RUN のため、キューには書き込みません。")
        return

    with QUEUE_PATH.open("a", encoding="utf-8") as handle:
        for line in new_lines:
            handle.write(line + "\n")
    print(f"\nキューに {len(new_lines)} 件追加しました。")


if __name__ == "__main__":
    main()
