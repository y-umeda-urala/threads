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
                     （neta/ネタ帳.md があるときは、そちらが優先される）
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

POLICY_CORE = """## 発信方針（2026-09-13 確定。ここが最優先の考え方）

読まれるかどうかは、書き出しが「外の話」か「自分たちの話」かで決まる。
同じアカウント・同じ書き手で、外の出来事から入った投稿は表示 2,820、
自社の作業手順から入った投稿は表示 6 だった。470 倍の差がある。

だから、投稿は次の順で組み立てる。

① 世の中・身近な出来事        ← 1行目。ここで読まれるかが決まる
② それによって起こる変化
③ 読者に発生する悩み・欲求     ← ここを飛ばすと「なんで？」になる
④ 必要になる行動
⑤ 商品・サービスにつながる解決策

**どこで止めるかは枠ごとに指定する（深さ A〜C）。指定より深く着地しないこと。**
- A: ①だけ。出来事の共有で終える
- B: ③まで。問題提起で止める（主力）
- C: ⑤まで。ただし商品名・サービス名・URLは書かない

すべての投稿を ⑤ まで着地させてはいけない。毎回着地させると ① が
売り込みの前振りに見え、① ごと読まれなくなる。

### 1行目の決まり
- 自分・自社・自社の商品の話で始めない
- 「〜しています」で始めない。断言か数字で始める
- 主語を「わたし」「うち」「当社」にしない
- 「作りました」「できました」で始めない（実測で平均の3分の1しか読まれない）

### つなげない話題
災害、事件、病気など、人の被害が絡む出来事は、商品にも自分たちのテーマにも
つなげない。論理が通っても感情が通らない。
"""

POLICY_ACCOUNT = """### この出来事を見るときに通す質問

**「この出来事は、福井の人・福井に関わる人が知っておきたいことになる？」**

ニュースや出来事の解説は書かない。自分たちは経済や社会の専門家ではない。
書くのは「で、この読者に何が起きるか」への翻訳だけ。

### 扱わないこと（2026-09-14 代表指示）

**採用・会社説明会・ミテカラの話題は、このアカウントでは扱わない。**
説明会の日程、採用担当の負担、応募者の理解度、動画での説明、外国人材への説明——
これらを入口にも着地にもしない。
"""

# 2026-09-21 代表指示。書き手枠（8/12/15/23）を紹介枠に変えたため全枠 A。
DEPTH = {6: "A", 8: "A", 10: "A", 12: "A", 14: "A", 15: "A", 17: "A", 19: "A", 21: "A", 23: "A"}

SLOTS = [
    (6, "福井の話題紹介", "F（紹介型）", "朝いちばんに読んで、今日の福井の話題を知れる"),
    (8, "福井の話題紹介", "F（紹介型）", "出勤前・始業前。今日これから動ける話題"),
    (10, "福井の話題紹介", "F（紹介型）", "通勤・通学時間に読まれる想定"),
    (12, "先の予定の紹介", "F（紹介型）", "昼休み。予定を立てはじめる時間。2週間以上先の催しを知らせる枠"),
    (14, "福井の話題紹介", "F（紹介型）", "お昼過ぎに読まれる想定"),
    (15, "福井の話題紹介", "F（紹介型）", "休憩時間。新店・季節の話題など軽く読めるもの"),
    (17, "福井の話題紹介", "F（紹介型）", "仕事終わりに読まれる想定"),
    (19, "行けるところまとめ", "G（まとめ型）", "夕方に、今日・明日どこへ行くかを決める人に読まれる。プロフィールで毎日19時と約束している枠"),
    (21, "先の予定の紹介", "F（紹介型）", "夜、ゆっくりした時間。2週間以上先の催しを知らせる枠"),
    (23, "福井の話題紹介", "F（紹介型）", "寝る前。明日・週末に行けるところ"),
]

MODEL_PREFERENCE = ("opus", "sonnet", "haiku")


LEARNINGS_PATH = Path("insights/learnings.md")


def learning_section() -> list[str]:
    """検証チーム（scripts/review.py）が毎日更新する指示を読む。無ければ何も足さない。"""
    if not LEARNINGS_PATH.exists():
        return []
    text = LEARNINGS_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [
        "## 検証チームからの指示（直近 7 日の数字に基づく）",
        "以下は実際の閲覧・反応の数字から決めた指示です。切り口・長さ・連投・話題の比重はこれに従ってください。",
        "ただし、運用ボードの文体・禁止事項・事実の扱いを超えることはできません。食い違えば運用ボードを優先します。",
        text,
        "",
    ]


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


NETA_PATH = Path("neta/ネタ帳.md")


def read_neta() -> str:
    """ネタ帳を読む。リポジトリの中にあれば、それを使う。

    2026-09-14 に置き場所を Google ドキュメントからこのリポジトリへ移した。
    毎朝の自動収集（.github/workflows/neta-collect.yml）がここに追記する。
    ファイルが無いときだけ、従来どおり NETA_DOC_ID のドキュメントを読む。
    移行の途中でも、どちらか読めたほうで動く。
    """
    if NETA_PATH.exists():
        text = NETA_PATH.read_text(encoding="utf-8")
        print(f"ネタ帳: {NETA_PATH} から {len(text)} 文字を読み込みました。")
        return text
    return fetch_doc(os.environ.get("NETA_DOC_ID", "").strip(), "ネタ帳")


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


def recent_texts(entries: list[dict], count: int | None = None) -> str:
    """直近の投稿を「日時・1 行目・使ったネタ」の形で返す。

    重複を避けるのが目的なので、本文全部ではなく 1 行目と note だけを渡す。
    件数は 1 日の枠数 × 7 日。1 日 10 本のアカウントでも 7 日ぶん見えるようにする
    （20 件固定だと 2 日ぶんしか見えず、同じネタが何度も出ていた。2026-09-18 修正）。
    """
    if count is None:
        count = max(len(SLOTS) * 7, 20)
    # キューはファイル順が時系列とは限らない（あとから別の枠を足すことがある）。
    # 予約時刻で並べ直し、まだ出ていないものは除いてから直近を取る。
    now = datetime.now(JST).isoformat()
    dated = [e for e in entries if isinstance(e.get("scheduled_at"), str)]
    past = sorted((e for e in dated if e["scheduled_at"] <= now), key=lambda e: e["scheduled_at"])
    parts = []
    for entry in past[-count:]:
        first = (entry.get("text", "") or "").split("\n")[0].strip()
        when = (entry.get("scheduled_at") or "")[5:16].replace("T", " ")
        note = (entry.get("note") or "").strip()
        line = f"- {when} ｜ {first}"
        if note:
            line += f"  〔{note}〕"
        parts.append(line)
    return "\n".join(parts)


def find_filled(entries: list[dict], target_date) -> dict[int, dict]:
    prefix = target_date.isoformat()
    filled: dict[int, dict] = {}
    for entry in entries:
        scheduled = entry.get("scheduled_at")
        if not isinstance(scheduled, str) or not scheduled.startswith(prefix):
            continue
        # 12:30 のような枠外の予約が 12:00 の枠を埋めたことにならないよう、
        # 分が 00 のものだけを「枠が埋まっている」とみなす
        if scheduled[14:16] != "00":
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
        f"- {hour}:00 ｜ 深さ: {DEPTH.get(hour, 'B')} ｜ 柱: {pillar} ｜ 型: {form} ｜ ねらい: {aim}"
        for hour, pillar, form, aim in needed
    )
    weekday = "月火水木金土日"[target_date.weekday()]
    hours = "、".join(f"{hour}:00" for hour, *_ in needed)
    hour_choices = "／".join(str(hour) for hour, *_ in SLOTS)
    already = describe_filled(filled)
    # 締めの問いの A/B。日付の偶数・奇数で切り替える。
    # 同じ枠を日をまたいで比べれば、時刻の影響を受けずに問いの効果だけを見られる。
    # 投稿 id に日付が入っているので、あとから振り分けを復元できる（別の記録は持たない）。
    if target_date.day % 2 == 0:
        AB_CLOSING_RULE = (
            "**今日は「問いあり」の日です（偶数日）。** 紹介枠は、本文または THREAD の最後に、"
            "読者が答えられる短い問いを1行置いてください。"
            "例：「行くなら土曜と日曜、どちらが空いていると思いますか。」「ここ、行ったことありますか。」"
        )
    else:
        AB_CLOSING_RULE = (
            "**今日は「問いなし」の日です（奇数日）。** 紹介枠は事実で締めてください。"
            "読者に問いかけない。「〜だそうです。」「〜が開かれます。」で終える"
            "（出典元の行は今まで通り付けます）。"
        )
   sections = [
        "あなたは、福井で制作と発信の仕事をしている個人です。Threads アカウント @fukui._.fukui の書き手。",
        "**会社の広報アカウントではありません（2026-09-19 変更）。** 一人称で、自分が見つけたこととして書きます。",
        "「当社」「弊社」と名乗らない。会社名を出さない。宣伝をしない。",
        "材料として日々URALA などのメディア記事を使いますが、自分が取材したようには書かず、出典を付けます。",
        f"{target_date.isoformat()}（{weekday}）の {hours} に投稿する {len(needed)} 本を書いてください。",
        "",
        "## 枠と役割",
        slot_lines,
        "",
        "柱が「福井の話題紹介」の枠は、福井県内の出来事・店・催し・展示を1件紹介する枠です。",
        "**日々URALA の記事に限りません（2026-09-15 変更）。** 材料はどちらから取ってもかまいません。",
        "",
        "  ① 下の「URALAサイトの新着記事」／「ウララコミュニケーションズの制作実績」",
        "  ② 下の「ネタ帳」にある福井の出来事（ふーぽ・フクブロ・自治体・主催者・店の公式など）",
        "",
        "### 12:00 と 21:00 は「先の予定」の枠（2026-09-21 代表指示）",
        "",
        "**この2枠は、2週間以上先の催しを扱います。** 今日・今週末のことは書きません。",
        "10本のうち2本を、先の予定にあてる枠です（19:00 のおすすめまとめも先の予定を扱います）。",
        "",
        "理由：今日の催しは、知った時点でもう動けないことがあります。",
        "先の予定は、読んだ人が予定を空けられる。保存やフォローにつながるのはこちらです。",
        "",
        "  ○ 10月10日と11日、一乗谷朝倉氏遺跡で「一乗谷文化祭」。入場無料だそうです。",
        "  ○ 11月14日と15日、越前町で「越前かにまつり」が開かれるそうです。",
        "  × 今日から2日間、○○で△△が開かれています。（これは他の枠の仕事）",
        "",
        "材料が2週間以上先のものしか無い枠なので、ネタ帳と URALA新着から",
        "**日付が先のものを探してください。** 見つからないときは、常設の展示や",
        "「〜まで開催中」の会期が先まで続くものでもかまいません。",
        "",
        "#### 先の予定は、何度出してもよい（2026-09-21 代表指示）",
        "",
        "**同じ催しを繰り返し出してかまいません。** むしろ何度も見かけることで、",
        "予定を立てるきっかけになります。「2日続けて同じ出来事を出さない」は",
        "**この2枠には当てはめません。**",
        "",
        "守ってほしいのは次の3つだけです。",
        "",
        "- **同じ催しを同じ日に2本出さない。** 12:00 と 21:00 は別の催しにする",
        "- **毎回、違うところを見せる。同じ書き出しを使い回さない**",
        "  1回目 いつ・どこで ／ 2回目 何ができるか ／ 3回目 誰と行くと楽しいか",
        "  4回目 去年はどうだったか ／ 5回目 行き方・混み具合・持ち物",
        "  同じ催しを同じ言い方で繰り返すと、読み飛ばされます。角度を変えれば繰り返せます",
        "- **材料が複数あるなら回す。** 1つしか無い日は、同じ催しでかまいません",
        "",
        "開催まで2週間を切ったものは、この枠からは外してください。",
        "そこからは 19:00 のおすすめまとめと、他の紹介枠が拾います。",
        "",
        "### 本文に地名と固有名詞を入れる（2026-09-18 代表指示）",
        "",
        "**紹介枠は、本文（連投の 1 本目）に必ず地名を入れます。**",
        "「福井」「鯖江」「越前」「小浜」「勝山」「敦賀」「坂井」「あわら」など、県名か市町名。",
        "**イベント名・店名・施設名も、できるだけ本文に入れてください。**",
        "",
        "固有名詞を thread に落とすと、本文だけでは何の話か分かりません。実際にそうなっていました。",
        "",
        "  × 日本一の眼鏡の産地で、2日間。",
        "  ○ 鯖江でめがねフェス2026。今週末の2日間です。",
        "",
        "  × 9月に入って、夏の記憶がすこし遠くなりました。",
        "  ○ 福井のシンガーソングライター・大越佑華さんが、夏を1枚のアルバムにしています。",
        "",
        "  × そのお店でしか買えない、というお菓子があります。",
        "  ○ 福井のロクメイカスタードに、そこでしか買えないお菓子があります。",
        "",
        "### 締めの問い（2026-09-19 から A/B で比べています）",
        "",
        AB_CLOSING_RULE,
        "",
        "理由：表示数が伸びても、いいね・返信の実数が 0 のままだと露出の天井が早く来ます。",
        "どちらが効くかを実測で決めるため、日によって出し分けています。**指示どおりに揃えてください。**",
        "（19:00 のまとめ枠は、この A/B の対象外です。毎日かならず問いで締めます）",
        "",
        "### 19:00 の「先の予定のおすすめ」（2026-09-21 代表指示で変更・毎日）",
        "",
        "**この枠だけは1件紹介ではなく、2〜3件を並べたまとめです。**",
        "**4日後以降の催しだけを扱います。** 今日・明日のことは書きません。",
        "プロフィールでも毎日19時と約束している枠です。**毎日必ず作ってください。**",
        "",
        "ねらいは、読んだ人が**予定を空けられる**ことです。今日の催しは、知った時点で",
        "もう動けないことがあります。先の予定なら、手帳を開いてもらえます。",
        "保存やフォローにつながるのはこちらです。",
        "月・火に出せば今週末が入り、水〜金に出せば来週の話になります。どちらでもかまいません。",
        "",
        "形：",
        "",
        "  本文（80字以内）… 何がいくつ載っているかを1行で言う。時期と件数を必ず入れる",
        "  THREAD 1件目以降 … 1件につき1つ。地名／名前／いつ／一言（→ 誰にどう効くか）／出典元：URL",
        "",
        "  例（本文）:",
        "    今週末から来週にかけて、福井で行けるところを3つ。鯖江・小浜・越前です。",
        "",
        "  例（THREAD の1件）:",
        "    鯖江｜めがねフェス2026（9/26-27）",
        "    めがねミュージアム周辺。産地の工場が開く2日間です。",
        "    出典元：https://example.com/...",
        "",
        "決めごと：",
        "",
        "- **4日後以降に開催されるものだけ。** 今日・明日・3日以内のものは入れない（それは他の枠の仕事）",
        "- **日付の近い順に並べる**",
        "- **同じ催しを2日続けて筆頭に置かない。** 12:00・21:00 で扱った催しと重なるのはかまいません（見せ方が違うため）",
        "- 2件に満たない日は、会期の長い展示・常設の施設で埋めてよい（4日後以降も開いているもの）",
        "- **市町をばらけさせる。** 同じ市から2件以上並べない（材料が無いときは可）",
        "- 1件ずつに**出典元のURL**を付ける。URLを作らない・推測しない",
        "- 商品の話はしない。この枠は深さ A（出来事の共有）で止める",
        "- 末尾に1行、問いを置く。例：「どれか予定に入れそうですか。」",
        "",
        "### この枠の書き方（2026-09-15 代表指示）",
        "",
        "**記事を引用している文体で書き、最後に出典を必ず付けます。**",
        "自分が取材したかのように書かないこと。「〜という記事が出ています」「〜によると」",
        "「〜と紹介されています」のように、どこかで読んだ話として書きます。",
        "",
        "本文または THREAD の最後に、次の形で1行を置いてください。",
        "",
        "  出典元：https://example.com/article",
        "",
        "- URL は材料に書かれているものをそのまま使う。**URLを作らない・推測しない**",
        "- 出典が無い材料は、この枠では使わない（柱を「お役立ち・問いかけ」に読み替える）",
        "- 記事本文を丸ごと写さない。引用するなら短く、事実（日付・場所・名前）を中心に",
        "- この枠に限り、下の文体ルールの「リンクは貼らない」を適用しません",
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
        *learning_section(),
        "## 運用ボード（最優先のルール。以下の指示と食い違ったらボードを優先する）",
        board or "（読み込めませんでした。以下の要点だけで書いてください）",
        "",
        "## ネタ帳（担当者本人が書いた生の材料。最優先で使う）",
        neta or "（空です）",
        "",
        "## URALAサイトの新着記事（記事紹介枠の材料。タイトルと概要の範囲で紹介し、内容を創作しない）",
        articles or "（取得できませんでした）",
        "",
        "## 制作実績（urala-design.jp。紹介枠の材料に使ってよい）",
        "**自分が関わった仕事として、一人称で書きます。** 会社名や「弊社の実績」という書き方はしない。",
        "例：× 弊社が制作した福井の◯◯様のサイト ／ ○ 福井の◯◯さんのサイトを作ったとき、",
        works or "（取得できませんでした）",
        "",
        "## 同じネタ・同じ投稿の使い回し（2026-09-18 代表指示）",
        "",
        "同じネタを何度使ってもかまいません。**連続させないことだけ守ってください。**",
        "",
        "- **同じ出来事（催し・店・記事）は、1 日に 2 本まで。**",
        "  2 本出すときは枠を 4 時間以上あける（6:00 と 14:00 は可。8:00 と 10:00 は不可）",
        "- **2 日続けて同じ出来事を出さない。** 1 日あける",
        "  ただし **開催日まで 3 日以内の催しは、毎日 1 本まで出してよい**（直前の告知は効くため）",
        "- **同じ書き出し（1 行目）を同じ日に 2 回使わない。** 角度を変える",
        "- **本文をそのまま出し直すのは、前回から 7 日以上あいていれば可。**",
        "  伸びた投稿の再掲は歓迎します。ネタが薄い日は、新しく薄いものを作るより再掲のほうがよい",
        "",
        "**ただし 12:00 と 21:00（先の予定の枠）は、この制限の外です。** 下に別のルールがあります。",
        "",
        "## 直近 7 日の投稿（日時・1 行目・使ったネタ）",
        "",
        "**ここに出ている出来事・記事・切り口は、上のルールに照らして使えるかを必ず確認すること。**",
        recent or "（なし）",
        "",
        POLICY_CORE,
        "",
        POLICY_ACCOUNT,
        "",
        "## 連投の 1 本目（本文）について（2026-09-18 代表指示）",
        "",
        "**本文だけを読んで、何の話か分かるように書いてください。**",
        "thread を読まなくても「何について」「誰に関係するか」が伝わること。",
        "",
        "これまで「80 字に入りきらない分は thread に回す」と指示していたため、",
        "本文が言いかけで終わり、何の話か分からない投稿が出ていました。**その指示は取り消します。**",
        "",
        "  × 9月に入って、夏の記憶がすこし遠くなりました。（何の話か分からない）",
        "  ○ 福井のシンガーソングライターが、夏を1枚のアルバムにしています。",
        "",
        "80 字以内は続けます。ただし **「入りきらない分を thread に回す」のではなく、",
        "「本文で言い切れる大きさまで話を絞る」** と考えてください。",
        "**thread は補足であって、本文の続きではありません。**",
        "",
        "## 文体の要点",
        "- 丁寧で落ち着いた敬語。です・ます調",
        "- 一文は短く。3〜4 行ごとに空行",
        "- 冒頭 1 行で引き込む",
        "- 絵文字は使わない。ハッシュタグは 0〜1 個",
        "- リンクは貼らない（例外は「福井の話題紹介」の枠。その枠は末尾に「出典元：URL」を必ず付ける）",
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
        "- text は 40〜120 字。**80 字以内を目標**にする。続きは thread に回す",
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
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        try:
            target_date = datetime.strptime(override, "%Y-%m-%d").date()
        except ValueError:
            fail(f"TARGET_DATE の形式が不正です: {override}（YYYY-MM-DD で指定してください）")
    else:
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
    # 当日ぶんを作り直すときに、すでに時刻を過ぎた枠を作らない
    # （過ぎた時刻で作ると、次の tick で即座に投稿されてしまうため）
    now_jst = datetime.now(JST)
    past = [
        slot[0]
        for slot in needed
        if datetime(target_date.year, target_date.month, target_date.day, slot[0], tzinfo=JST) <= now_jst
    ]
    if past:
        print("すでに時刻を過ぎているため作らない枠: " + "、".join(f"{h}:00" for h in past))
        needed = [slot for slot in needed if slot[0] not in past]
        if not needed:
            print("作れる枠がありません。何もしません。")
            return

    print("これから作る枠: " + "、".join(f"{h}:00" for h, *_ in needed))

    board = fetch_doc(os.environ.get("BOARD_DOC_ID", "").strip(), "運用ボード")
    neta = read_neta()
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
        if post.get("note"):
            item["note"] = str(post["note"])[:120]
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
