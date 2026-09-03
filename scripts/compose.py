"""翌日ぶんの Threads 投稿 3 本を作成し、投稿キューに追加する。

外部 cron から毎日きまった時刻（例: 22:00 JST）に起動される想定。

材料:
  - 運用ボード（Google ドキュメント / リンクを知っている全員が閲覧可）
  - ネタ帳（同上）
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
QUEUE_PATH = Path("posts/queue.jsonl")
API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"

# 予約する時刻と、その枠の役割
SLOTS = [
    (6, "ノウハウ／仕事観", "B（ノウハウ型）またはA（気づき型）", "起き抜けに読んで学びになる"),
    (12, "制作の裏側／福井というローカル", "C（裏側型）またはA（気づき型）", "何をしている人かが自然に伝わる"),
    (20, "自己開示・日常", "E（問いかけ型）またはA（気づき型）", "人柄が伝わり、返信が生まれる"),
]

# 未指定のときに上から順に探すモデル
MODEL_PREFERENCE = ("opus", "sonnet", "haiku")


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def fetch_doc(doc_id: str, label: str) -> str:
    """Google ドキュメントをプレーンテキストで取得する。

    「リンクを知っている全員が閲覧可」になっていれば認証なしで読める。
    読めなくても処理は止めず、その材料なしで続ける。
    """
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
    except Exception as exc:  # noqa: BLE001 - 材料が欠けても続行する
        print(f"::warning::{label}: 取得に失敗しました ({exc})")
        return ""
    print(f"{label}: {len(text)} 文字を読み込みました。")
    return text


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
    except Exception as exc:  # noqa: BLE001
        fail(f"Anthropic API に接続できませんでした: {exc}")
    return {}


def pick_model(api_key: str) -> str:
    """使うモデルを決める。指定がなければ利用可能なものから選ぶ。"""
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


def recent_texts(entries: list[dict], count: int = 12) -> str:
    """直近の投稿を、重複回避の材料として1つの文字列にまとめる。"""
    parts = []
    for entry in entries[-count:]:
        text = entry.get("text", "")
        thread = " ".join(entry.get("thread") or [])
        parts.append(f"- {text} {thread}".strip())
    return "\n".join(parts)


def find_filled(entries: list[dict], target_date) -> dict[int, dict]:
    """対象日にすでに予約が入っている枠を、時 -> 投稿 の形で返す。"""
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


def build_prompt(board: str, neta: str, recent: str, target_date, needed, filled) -> str:
    slot_lines = "\n".join(
        f"- {hour}:00 ｜ 柱: {pillar} ｜ 型: {form} ｜ ねらい: {aim}"
        for hour, pillar, form, aim in needed
    )
    weekday = "月火水木金土日"[target_date.weekday()]
    hours = "、".join(f"{hour}:00" for hour, *_ in needed)
    already = describe_filled(filled)
    sections = [
        "あなたは YU さん（福井市のフリーランス Web クリエイター／SNS コンテンツ制作者）の",
        "Threads 発信チームの編集担当です。",
        f"{target_date.isoformat()}（{weekday}）の {hours} に投稿する {len(needed)} 本を書いてください。",
        "",
        "## 枠と役割",
        slot_lines,
        "",
    ]
    if already:
        sections += [
            "## 同じ日にすでに入っている投稿（YU さん本人が用意したもの）",
            "これらとネタ・切り口・書き出しが重ならないようにしてください。",
            "文体もこれらに寄せてください。",
            already,
            "",
        ]
    sections += [
        "## 運用ボード（最優先のルール。以下の指示と食い違ったらボードを優先する）",
        board or "（読み込めませんでした。以下の要点だけで書いてください）",
        "",
        "## ネタ帳（YU さん本人が書いた生の材料。最優先で使う）",
        neta or "（空です）",
        "",
        "## 直近の投稿（ネタ・切り口・書き出しの重複を避けるため）",
        recent or "（なし）",
        "",
        "## 文体の要点",
        "- 丁寧で落ち着いた敬語。です・ます調",
        "- 一文は短く。3〜4 行ごとに空行",
        "- 冒頭 1 行で引き込む",
        "- 絵文字は使わない。ハッシュタグは 0〜1 個",
        "- リンクは貼らない",
        "- 1 投稿につき伝えたいことは 1 つだけ",
        "- クライアント実名は出さない（「福井の解体業の会社さん」のように業種で表現する）",
        "- 金額・社内事情・未公開情報は書かない",
        "- 誇張しない、盛らない。自慢に読めないよう、学び・失敗・裏側の形で語る",
        "",
        "## 事実について（最重要）",
        "確認できない事実を創作しないこと。ネタ帳・運用ボード・直近の投稿に根拠がある内容だけを書く。",
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
        "HOUR には 6 / 12 / 20 のいずれかの数字だけを書きます。",
    ]
    return "\n".join(sections)


def parse_posts(text: str) -> list[dict]:
    """@@@POST 〜 @@@END の組を読み取る。

    JSON をやめたのは、本文に改行と空行が入るため。モデルが改行をそのまま書くと
    JSON として壊れ、投稿が 1 本も作られない日が出た。区切り記号なら改行は素通りする。
    """
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
    """1 度目で本数が揃わなければ、形式を念押しして 1 度だけやり直す。"""
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

    # すでに埋まっている枠は触らず、空いている枠だけを作る
    filled = find_filled(entries, target_date)
    needed = [slot for slot in SLOTS if slot[0] not in filled]
    if filled:
        print("すでに予約済みの枠: " + "、".join(f"{h}:00" for h in sorted(filled)))
    if not needed:
        print(f"{target_date} は 3 枠とも埋まっています。何もしません。")
        return
    print("これから作る枠: " + "、".join(f"{h}:00" for h, *_ in needed))

    board = fetch_doc(os.environ.get("BOARD_DOC_ID", "").strip(), "運用ボード")
    neta = fetch_doc(os.environ.get("NETA_DOC_ID", "").strip(), "ネタ帳")

    model = pick_model(api_key)
    prompt = build_prompt(board, neta, recent_texts(entries), target_date, needed, filled)
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
