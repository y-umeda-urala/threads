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

import お得日
import 宿

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
    (6, "今日で終わるものまとめ", "G（まとめ型）", "朝いちばん。今日が最終日の催しを知らせる。該当が無い日はふつうの紹介に差し替える"),
    (8, "福井の話題紹介", "F（紹介型）", "出勤前・始業前。今日これから動ける話題"),
    (10, "福井の話題紹介", "F（紹介型）", "通勤・通学時間に読まれる想定"),
    (12, "先の予定の紹介", "F（紹介型）", "昼休み。予定を立てはじめる時間。2週間以上先の催しを知らせる枠"),
    (14, "福井の話題紹介", "F（紹介型）", "お昼過ぎに読まれる想定"),
    (15, "楽天トラベルの紹介（PR）", "F（紹介型）", "3アカウント共通の枠。同じ日は同じ内容を出し、アカウントごとの効き方を比べる。5と0のつく日はクーポン1本の短文、それ以外は宿のまとめ"),
    (17, "福井の話題紹介", "F（紹介型）", "仕事終わりに読まれる想定"),
    (19, "先の予定の紹介", "F（紹介型）", "夜のはじめ。2週間以上先の催しを知らせる枠"),
    (21, "先の予定のおすすめ", "G（まとめ型）", "夜、この先の予定を決める人に読まれる。4日後以降の催しを2〜3件並べる。プロフィールで毎日21時と約束している枠"),
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


def build_prompt(board: str, neta: str, articles: str, works: str, recent: str, target_date, needed, filled, hotel=None, hotel_hour=None, mugi=None) -> str:
    def _枠の行(hour, pillar, form, aim):
        行 = f"- {hour}:00 ｜ 深さ: {DEPTH.get(hour, 'B')} ｜ 柱: {pillar} ｜ 型: {form} ｜ ねらい: {aim}"
        # PR の枠は、9枠ぶんの指示に埋もれて読み飛ばされることがある。
        # 枠の一覧そのものに印を出して、見落としを防ぐ（2026-09-25）。
        if hotel_hour is not None and hour == hotel_hour:
            行 += ("\n  ★★ この枠は下の「PR」の節の指示だけに従ってください。"
                   "**本文を必ず【PR】で始めること。** 上の柱・型の指示は当てはめません ★★")
        return 行

    slot_lines = "\n".join(_枠の行(*x) for x in needed)
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
        *(
            [
                f"**ただし {hotel_hour}:00 の枠だけは例外です。**"
                "この枠は楽天トラベルの紹介（PR）で、広告として出します。",
                "下の「PR」の節の指示に従い、**本文を必ず【PR】で始めてください。**",
                "「宣伝をしない」はこの枠には当てはめません（ステマ規制のため、"
                "広告であることを隠すほうが問題になります）。",
            ]
            if hotel_hour is not None
            else []
        ),
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
        "### 12:00 と 19:00 は「先の予定」の枠（2026-09-21 代表指示）",
        "",
        "**この2枠は、2週間以上先の催しを扱います。** 今日・今週末のことは書きません。",
        "10本のうち2本を、先の予定にあてる枠です（21:00 のおすすめまとめも先の予定を扱います）。",
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
        "**同じ催しを、日をまたいで繰り返し出してかまいません。** むしろ何度も",
        "見かけることで、予定を立てるきっかけになります。",
        "「2日続けて同じ出来事を出さない」は**この2枠には当てはめません。**",
        "",
        "ただし **同じ日に 2 回は出せません。** 12:00 と 19:00 では違う催しを選ぶこと",
        "（2026-09-23 代表指示。判定は「出典元：」の URL）。",
        "",
        "守ってほしいのは次の3つだけです。",
        "",
        "- **同じ催しを同じ日に2本出さない。** 12:00 と 19:00 は別の催しにする",
        "- **毎回、違うところを見せる。同じ書き出しを使い回さない**",
        "  1回目 いつ・どこで ／ 2回目 何ができるか ／ 3回目 誰と行くと楽しいか",
        "  4回目 去年はどうだったか ／ 5回目 行き方・混み具合・持ち物",
        "  同じ催しを同じ言い方で繰り返すと、読み飛ばされます。角度を変えれば繰り返せます",
        "- **材料が複数あるなら回す。** 1つしか無い日は、同じ催しでかまいません",
        "",
        "開催まで2週間を切ったものは、この枠からは外してください。",
        "そこからは 21:00 のおすすめまとめと、他の紹介枠が拾います。",
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
        "（21:00 のまとめ枠は、この A/B の対象外です。毎日かならず問いで締めます）",
        "",
        "### 6:00 の「今日で終わるもの」（2026-09-23 代表指示・新設）",
        "",
        "**今日が最終日の催しを、3〜5件並べるまとめです。**",
        "会期が今日で切れるもの、今日までの展示、今日が最終日の祭り。それだけを扱います。",
        "",
        "ねらいは、**間に合ううちに知らせる**ことです。同じ知らせでも、夕方に出したら",
        "その日はもう動けません。朝いちばんなら、今日の予定に入れられます。",
        "21:00 のおすすめまとめが「これから」を扱うので、6:00 は「今日まで」を受け持ちます。",
        "",
        "**すでに紹介した催しでも、今日が最終日なら入れてかまいません。**",
        "最後に知らせる意味があるので、使い回しの制限はこの枠にはかけません。",
        "",
        "形：",
        "",
        "  本文 … 見出し1行＋**催しの一覧をそのまま並べる**（リンクは入れない）",
        "  THREAD 1件目以降 … 1件につき1つ。地名／名前／一言／出典元：URL",
        "",
        "  例（本文）:",
        "    【福井】今日で終わる催し4つ",
        "",
        "    ① 🦕 越前大仏「雲海特別観覧」（勝山市）",
        "    ② ⚓ 敦賀まつり（敦賀市）",
        "    ③ 🌊 越前がにミュージアム企画展（越前町）",
        "    ④ 🏯 丸岡城ライトアップ（坂井市）",
        "",
        "  例（THREAD の1件）:",
        "    勝山｜越前大仏「雲海特別観覧」（9/21-23）",
        "    今日まで拝観料なしで入れるそうです。朝の時間が空いている人に。",
        "    出典元：https://example.com/...",
        "",
        "決めごと：",
        "",
        "- **今日が最終日のものだけ。** 明日以降も続くものは入れない（それは他の枠の仕事）",
        "- 「〜まで開催中」「9/21-23」のような会期から、**今日が最後かどうかを自分で判断する**",
        "- **市町をばらけさせる。** 同じ市から2件以上並べない（材料が無いときは可）",
        "- 1件ずつに**出典元のURL**を付ける。URLを作らない・推測しない",
        "- 商品の話はしない。この枠は深さ A（出来事の共有）で止める",
        "",
        "**該当が3件に満たない日は、この枠をやめて、ふつうの「福井の話題紹介」",
        "（型 F・1件紹介）を書いてください。** 無理に集めて、明日も続くものを",
        "「今日まで」と書くのがいちばんまずい。**間違った締切を出すくらいなら、やめる。**",
        "1〜2件しか無い日も、まとめにせず1件紹介にしてかまいません。",
        "",
        "### 21:00 の「先の予定のおすすめ」（2026-09-23 代表指示で 19:00 から移動・毎日）",
        "",
        "**この枠だけは1件紹介ではなく、2〜3件を並べたまとめです。**",
        "**4日後以降の催しだけを扱います。** 今日・明日のことは書きません。",
        "プロフィールでも毎日21時と約束している枠です。**毎日必ず作ってください。**",
        "",
        "ねらいは、読んだ人が**予定を空けられる**ことです。今日の催しは、知った時点で",
        "もう動けないことがあります。先の予定なら、手帳を開いてもらえます。",
        "保存やフォローにつながるのはこちらです。",
        "月・火に出せば今週末が入り、水〜金に出せば来週の話になります。どちらでもかまいません。",
        "",
        "形：",
        "",
        "  本文 … 見出し1行＋**催しの一覧をそのまま並べる**（リンクは入れない）",
        "  THREAD 1件目以降 … 1件につき1つ。地名／名前／いつ／一言（→ 誰にどう効くか）／出典元：URL",
        "",
        "  例（本文）:",
        "    【福井】来週末に行けるところ3選",
        "",
        "    ① 👓 めがねフェス2026（鯖江市・9/26-27）",
        "    ② 🐟 若狭おばま食のまつり（小浜市・10/4）",
        "    ③ 🖌 越前秋季陶芸祭（越前町・10/3-4）",
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
        "- **同じ催しを2日続けて筆頭に置かない。** 12:00・19:00 で扱った催しと重なるのはかまいません（見せ方が違うため）",
        "- 2件に満たない日は、会期の長い展示・常設の施設で埋めてよい（4日後以降も開いているもの）",
        "- **市町をばらけさせる。** 同じ市から2件以上並べない（材料が無いときは可）",
        "- 1件ずつに**出典元のURL**を付ける。URLを作らない・推測しない",
        "- 商品の話はしない。この枠は深さ A（出来事の共有）で止める",
        "- 末尾に1行、問いを置く。例：「どれか予定に入れそうですか。」",
        "",
        "",
        "### まとめ枠（6:00 と 21:00）の本文の作り方（2026-09-25 代表指示）",
        "",
        "**本文に一覧をそのまま出してください。予告だけにしないこと。**",
        "",
        "これまでは本文を「福井で行けるところを3つ。越前町・大野市・敦賀です。」のような",
        "予告にして、中身を THREAD に置いていました。**その形は再生が伸びませんでした。**",
        "",
        "  まとめ型 4本 … 再生の中央値 50（32〜62）",
        "  紹介型 130本 … 再生の中央値 358",
        "",
        "同じ日に出た楽天トラベルの一覧投稿は841再生で、こちらは本文に一覧が入っています。",
        "リンクの本数が原因ではありません（その投稿は返信に7本のリンクを持っています）。",
        "**本文に中身が無いと、読む人が止まらない**のが理由だと考えています。",
        "",
        "形：",
        "",
        "  1行目 … 「【福井】」で始まる見出し。最後に「◯選」か件数を入れる",
        "  空行",
        "  ① 絵文字 名前（市町・日付）",
        "  ② …",
        "",
        "- **絵文字は土地の目印にする。** 飾りではなく、どこの話か分かるように",
        "  （例：福井市 🏙／あわら・三国 ♨️／勝山・大野 🦕／鯖江 👓／越前市 🖌／",
        "   越前海岸 🌊／敦賀 ⚓／小浜 🐟／坂井 🏯／永平寺 🛕）",
        "- **本文に URL を入れない。** 出典元は THREAD 側に置く",
        "- 本文は Threads の上限（500字）に収める。入らなければ件数を減らす",
        "- THREAD は今まで通り。1件につき1つ、出典元のURLを必ず付ける",
        "",
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
        "## 本文の末尾（2026-09-25 代表指示）",
        "",
        f"**本文のいちばん最後に、1行あけて「{検索語}」と置いてください。**",
        "Threads の検索で引っかかるようにするためです。ハッシュタグにはしません。",
        f"書き忘れてもこちらで足しますが、文の流れを見て置いてもらえると自然になります。",
        f"（{HOTEL_HOUR}:00 の PR の枠には付けません）",
        "",
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
        "同じネタを何度使ってもかまいません。**同じ日に重ねないことだけ守ってください。**",
        "",
        "- **同じ出来事（催し・店・記事）は、1 日に 1 本まで（2026-09-23 代表指示）。**",
        "  **同じかどうかは「出典元：」の URL で見ます。**",
        "  同じ URL を 1 日に 2 本以上使わないこと。時間を空ければよい、ではありません",
        "  （これまでの「1 日 2 本まで・4 時間以上あける」は廃止しました）",
        "- **2 日続けて同じ出来事を出さない。** 1 日あける",
        "  ただし **開催日まで 3 日以内の催しは、毎日 1 本まで出してよい**（直前の告知は効くため）",
        "- **同じ書き出し（1 行目）を同じ日に 2 回使わない。** 角度を変える",
        "- **本文をそのまま出し直すのは、前回から 7 日以上あいていれば可。**",
        "  伸びた投稿の再掲は歓迎します。ネタが薄い日は、新しく薄いものを作るより再掲のほうがよい",
        "",
        "**12:00 と 19:00（先の予定の枠）も、1 日 1 本の決まりは守ります。**",
        "この 2 枠は「日をまたいで何度でも出してよい」という意味で制限の外であって、",
        "**同じ日に同じ催しを 2 回出してよい、という意味ではありません。**",
        "12:00 と 19:00 で違う催しを選んでください。",
        "",
        "**21:00 のまとめ枠も同じです。** まとめに入れる催しは、その日の他の枠で",
        "使っていないものから選んでください。",
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
    if mugi and hotel_hour is not None:
        sections += [
            f"## {hotel_hour}:00 の枠は、クーポンのお知らせです（楽天トラベル・PR）",
            "",
            f"今日は楽天トラベルの「{mugi['お得日']['名']}」。{mugi['お得日']['何が']}。",
            f"{mugi['お得日']['条件']}",
            *([f"うたい文句: {mugi['うたい文句']}"] if mugi["うたい文句"] else []),
            "",
            "**この枠は、宿を並べません。クーポンの話だけを短く書きます。**",
            "代表共有のnote記事で、2日で66,102円になった型です。",
            "型は「誰向け ＋ どんなお得 ＋ 期限」。",
            "",
            *むぎの決まり(mugi),
            "",
        ]
    if hotel and hotel_hour is not None:
        # 5と0のつく日は、楽天トラベルのクーポンが出る（エントリー不要）。
        旅の得 = お得日.旅(target_date)
        sections += [
            f"## {hotel_hour}:00 の枠は、宿のまとめです（楽天トラベル・PR）",
            "",
            *(
                [
                    f"今日は楽天トラベルの「{旅の得['名']}」（{旅の得['何が']}）。",
                    "クーポンの案内は返信にこちらで付けるので、見出しには書かないでください。",
                    "",
                ]
                if 旅の得
                else []
            ),
            "今日並べる宿（こちらで組み立てます。あなたは見出しだけ書いてください）:",
            *[f"- {行}" for 行 in hotel["一覧"]],
            "",
            *宿の決まり(hotel),
            "",
        ]
    if not hotel and hotel_hour is None:
        sections += [
            "## 今日は宿の紹介をしません",
            "",
            "紹介できる宿が用意されていません。**どの枠でも宿の紹介を書かないでください。**",
            "宿の名前を出して良さを伝える書き方をしない。予約をすすめる書き方をしない。",
            "",
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


def generate(api_key: str, model: str, prompt: str, hours: list[int]) -> list[dict]:
    """足りない枠だけを聞き直しながら、作れたぶんを集める。

    2026-09-23 まで「全部そろわなければ失敗」だった。9本できていても全部捨てて
    翌日の投稿が0本になる事故が2晩続いたため、**作れたぶんは使う**ように変えた。
    半分に満たないときだけ失敗させる。
    """
    expected = len(hours)
    集まった: dict[int, dict] = {}

    for attempt in (1, 2, 3):
        if attempt == 1:
            この回の指示 = prompt
        else:
            残り = [h for h in hours if h not in 集まった]
            この回の指示 = (
                prompt
                + "\n\n---\n直前の返答では枠が足りませんでした。"
                + "**足りないのは "
                + "、".join(f"{h}:00" for h in 残り)
                + " です。この枠だけを作ってください。**"
                + "説明や ``` を付けず、@@@POST 〜 @@@END の組だけを返してください。"
            )
        text = ask(api_key, model, この回の指示)
        for post in parse_posts(text):
            try:
                hour = int(post["hour"])
            except (TypeError, ValueError):
                continue
            if hour in hours and hour not in 集まった and (post.get("text") or "").strip():
                集まった[hour] = post
        if len(集まった) >= expected:
            return [集まった[h] for h in hours]
        print(f"::warning::{attempt} 回目まで: {expected} 本のはずが {len(集まった)} 本です。")

    足りない = [h for h in hours if h not in 集まった]
    if len(集まった) < max(1, (expected + 1) // 2):
        fail(
            f"{expected} 本のうち {len(集まった)} 本しか作れませんでした（3 回試行）。"
            f"半分に満たないので中止します。\n--- 最後の生の出力 ---\n{text[:1200]}"
        )
    print(
        f"::warning::{expected} 本のうち {len(集まった)} 本で進めます。"
        f"作れなかった枠: " + "、".join(f"{h}:00" for h in 足りない)
    )
    return [集まった[h] for h in hours if h in 集まった]


# 「出典元：」に書かれた URL を取り出す。
# 同じ催しを 1 日に 2 本出していないかは、この URL で見る（2026-09-23 代表指示）。
# 本文の言い回しは変えられても、出典は変えられないので、これがいちばん確かな鍵になる。
SOURCE_URL = re.compile(r"出典元[：:]\s*(https?://\S+)")


def source_urls(text: str, thread: list[str]) -> set[str]:
    found = set()
    for part in [text or "", *(thread or [])]:
        for url in SOURCE_URL.findall(part):
            found.add(url.rstrip("）)、。,. "))
    return found



# 宿の紹介枠（2026-09-23 代表指示）。楽天トラベルのアフィリエイト。
# どの宿をどの切り口で出すかは scripts/宿.py が日付から決める。
# 3アカウントとも同じリスト・同じ計算なので、同じ日には同じ内容になる。
# 本文の末尾に必ず入れる語（2026-09-25 代表指示）。
# Threads の検索で引っかかるようにするため。PR の枠には付けない。
検索語 = "福井イベント"

# 1本のまとめに何軒並べるか。
宿の軒数 = 7

HOTEL_HOUR = 15

# 【PR】の印。本文の先頭に無ければ止める（ステマ規制）。
if "PR_MARKERS" not in dir():
    PR_MARKERS = ("【PR】", "#PR", "＃PR", "[PR]")
if "URL_IN_TEXT" not in dir():
    URL_IN_TEXT = re.compile(r"https?://\S+")

# 泊まっていない宿を「泊まった」と書かせない。
# 楽天トラベルに載っている情報を読んで書くだけなので、体験として書くと嘘になる。
STAYED_VOICE = re.compile(
    r"泊まっ(た|て)|宿泊した|行ってき|訪れた|使ってみ|入ってみ|食べてき"
)


# 宿の枠の2つの型（2026-09-24 代表判断）。どちらが稼ぐかを数字で比べる。
#
#   むぎ型 … クーポン1本だけ。40〜90字。代表共有のnote記事で
#             2日66,102円になった型（誰向け ＋ どんなお得 ＋ 期限）。
#   9選型 … 宿を7軒並べて、返信に1軒ずつリンク。到達は取れている型
#            （代表が見つけた投稿は表示1.5万・いいね570）。
#
# 5と0のつく日は むぎ型、それ以外は 9選型 にする。
# むぎ型の力は「期限」にあり、期限が作れるのはクーポンが出る日だけのため。
# 成果は楽天アフィリエイトのレポートで分かれて見える
# （むぎ型＝クーポンのリンク、9選型＝宿のリンク）。


def 宿の型(対象日) -> str:
    """その日の宿の枠をどちらの型で書くか。"""
    return "むぎ" if お得日.旅(対象日) else "9選"


def むぎの決まり(材料: dict) -> list[str]:
    """むぎ型で守ってもらう決まり。"""
    数 = 材料.get("うたい文句") or ""
    return [
        "1. **本文の冒頭を必ず「【PR】」で始める。** 末尾ではなく先頭です（ステマ規制）",
        "2. **1行目で「誰に向けた話か」をはっきり書く。**",
        "   例：「週末に福井へ泊まる人へ」「連休に子どもと出かける人へ」。",
        "   全員に向けて書かないこと。宛先がはっきりしている投稿ほど読まれます",
        "3. **どんなお得かを一言で。**",
        (f"   書いてよい数字は「{数}」だけです。これ以外の数字を作らないこと"
         if 数 else "   数字は書かないでください。渡していません"),
        f"4. **いつまでかを書く（{材料.get('期限') or 'この日から48時間'}）。**",
        "   いま見る理由になります。ここが無いと後回しにされます",
        "5. **エントリーは要らないと書いてよい。** ただし",
        "   「クーポンは自分で取りに行く必要がある」ことも書いてください",
        "6. **宿の名前を出さない。** この枠はクーポンの話だけです",
        "7. **URL は絶対に書かない。** リンクは返信（コメント欄）にこちらで付けます",
        "8. 「泊まった」「行ってきた」と書かない",
        "",
        "**本文は【PR】を含めて日本語 40〜90 字。短いほど読まれます。**",
        "note記事で20.2万表示・66,102円になった投稿は37字でした。長く書かないこと。",
        "thread は空のままにしてください。返信はこちらで付けます。",
    ]


def むぎの返信(材料: dict) -> list[str]:
    """むぎ型の返信。クーポンのリンク1本だけ。"""
    行 = ["PR", "楽天トラベルのクーポンはこちらです", ""]
    for c in 材料["クーポン"][:2]:
        行.append(f"{c['名']}\n{c['url']}")
        行.append("")
    return ["\n".join(行).strip()]


def むぎの数字(text: str, 材料: dict) -> str | None:
    """本文にある数字のうち、渡したうたい文句に無いものを返す。"""
    許す = str(材料.get("うたい文句") or "") + str(材料.get("期限") or "")
    許す = 許す.replace("％", "%").replace(" ", "")
    for m in re.finditer(r"\d+(?:\.\d+)?\s*(?:割|%|％|倍|円|時間|日|泊)", text):
        語 = m.group(0).replace("％", "%").replace(" ", "")
        if 語 in 許す:
            continue
        return m.group(0)
    return None


def 宿の決まり(選んだ: dict) -> list[str]:
    """宿の枠で守ってもらう決まり。

    2026-09-23、代表が見つけた「よく伸びている楽天トラベルの投稿」に合わせた。
      1本目 … 「【福井】◯◯な宿7選」＋ 宿名の一覧だけ。リンクを入れない
      返信  … 冒頭に「PR」。宿ごとに一文とリンク
    本文にリンクを入れると表示が落ちるので、リンクは返信に置く。
    一覧は保存されやすく、返信まで読んだ人がリンクを踏む、という流れ。

    AI に書いてもらうのは **1行目の見出しだけ**。
    宿の一覧・一文・リンクは、こちらがデータから組み立てる
    （宿の名前を書き間違えたり、無い宿を足したりしないため）。
    """
    return [
        "**この枠で書くのは、1行目の見出しだけです。** 他は何も書かないでください。",
        "",
        f"今日の切り口: {選んだ['name']}",
        "",
        "見出しの決まり",
        "",
        "1. **「【福井】」で始める。** そのあとに、どんな宿を集めたかを書く",
        "2. **最後に「◯選」と軒数を入れる。**",
        f"   今日は {選んだ['軒数']} 軒なので「{選んだ['軒数']}選」です",
        "3. **日本語で 30 字まで。** 1行だけ。改行しない",
        "4. **URL・【PR】・絵文字・ハッシュタグは書かない。** こちらで付けます",
        "5. **「ない」と書かない。** 楽天トラベルに載っていないのは",
        "   「設備が無い」ではなく「宿が登録していない」かもしれません",
        "6. **泊まった体で書かない。** この宿には泊まっていません",
        "",
        "例（そのまま使わず、今日の切り口に合わせて書いてください）",
        "  【福井】夜遅く着いても入れる宿7選",
        "  【福井】サウナがある宿7選",
        "  【福井】朝ごはんの場所が分かる宿7選",
        "",
        "thread は空のままにしてください。返信はこちらで付けます。",
    ]


def 宿の本文(まとめ: dict, 見出し: str) -> str:
    """1本目。見出し＋宿の一覧。リンクは入れない。

    宿の名前が長い日に上限を超えることがあるので、入らなければ下から減らす。
    見出しの「◯選」と数が食い違わないよう、数もここで直す。
    """
    宿たち = list(まとめ["宿"])
    while 宿たち:
        削り = {**まとめ, "宿": 宿たち}
        直した = 見出し.strip()
        for 数 in range(len(まとめ["宿"]), 0, -1):
            直した = 直した.replace(f"{数}選", f"{len(宿たち)}選")
        本 = 直した + "\n\n" + "\n".join(宿.一覧の行(削り))
        if リンクの長さ(本) <= リンクの上限 or len(宿たち) <= 3:
            return 本
        宿たち = 宿たち[:-1]
    return 見出し.strip()


def 宿の返信(まとめ: dict, 対象日) -> list[str]:
    """返信。冒頭に PR を置き、宿ごとの一文とリンクを並べる。

    楽天アフィリエイトの決まりでは、広告表記はファーストビューに置く。
    だから返信の1行目を「PR」にする。
    """
    行たち = 宿.返信の行(まとめ)
    頭 = "PR\n楽天トラベルのページはこちらです"
    出, いま, 先頭 = [], 頭, True
    for 行 in 行たち:
        つぎ = いま + "\n\n" + 行
        if リンクの長さ(つぎ) > リンクの上限:
            出.append(いま)
            いま = "PR\n\n" + 行
        else:
            いま = つぎ
        先頭 = False
    if いま:
        出.append(いま)

    # クーポン。代表が楽天アフィリエイトの管理画面で作ったリンクだけを使う。
    # 「5と0のつく日」のものは、その日だけ出す。
    クーポン = 宿.クーポンを読む()
    きょう旅 = お得日.旅(対象日)
    使う = [
        c for c in クーポン
        if str(c.get("いつ", "いつでも")) != "5と0のつく日" or きょう旅
    ]
    if 使う:
        塊 = ["PR", "楽天トラベルのクーポンはこちらです"]
        if きょう旅:
            塊.append("今日は5と0のつく日。エントリーは要りません")
        塊.append("")
        for c in 使う[:3]:
            塊.append(f"{c['名']}\n{c['url']}")
            塊.append("")
        出.append("\n".join(塊).strip())
    return 出


リンクの長さ = len
リンクの上限 = 480  # Threads は 500 字


def 宿のリンク(選んだ: dict) -> list[str]:
    """宿のリンクを、連投1本にまとめられるだけまとめる。

    本文にURLを入れると表示が落ちるので、リンクは連投（コメント欄）に置く。
    連投は少ないほうが読まれるので、入るだけ1本にまとめる。
    """
    かたまり, いま = [], ""
    for 行 in 選んだ.get("links") or []:
        つぎ = (いま + "\n\n" + 行) if いま else 行
        if いま and リンクの長さ(つぎ) > リンクの上限:
            かたまり.append(いま)
            いま = 行
        else:
            いま = つぎ
    if いま:
        かたまり.append(いま)
    return かたまり


class 枠を落とす(Exception):
    """この枠だけ作らない。他の枠は残す。

    2026-09-24 と 25 に、1本の見張り違反で fail() が走り、その日の10本が
    まるごと作られなかった。1本の取りこぼしと、1日の全滅は釣り合わない。
    """


def 落とす(わけ: str):
    raise 枠を落とす(わけ)


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

    # 宿のまとめ枠（2026-09-23 代表指示）。毎日 1 本、HOTEL_HOUR の枠だけ。
    # どの切り口で何軒並べるかは scripts/宿.py が日付から決める。
    # 3アカウントとも同じリスト・同じ計算なので、同じ日には同じ宿が並ぶ。
    宿たち = 宿.読む()
    hotel = None
    if 宿たち and any(hour == HOTEL_HOUR for hour, *_ in needed):
        まとめ = 宿.今日のまとめ(target_date, 宿たち, いくつ=宿の軒数)
        if まとめ:
            hotel = {
                "name": まとめ["切り口"]["問い"],
                "軒数": len(まとめ["宿"]),
                "一覧": 宿.一覧の行(まとめ),
                "raw": まとめ,
            }
            print(
                f"宿の枠: {HOTEL_HOUR}:00 ｜ {hotel['name']}"
                f"（{hotel['軒数']} 軒／リスト {len(宿たち)} 軒）"
            )
        else:
            print("::warning::今日は 5 軒そろう切り口がありません。宿の紹介はしません。")
    elif 宿たち:
        print(f"宿の枠: {HOTEL_HOUR}:00 はすでに埋まっているため、今回は紹介しません。")
    else:
        print("::warning::宿のリストが空です。宿の紹介はしません。")

    # むぎ型（クーポン1本だけの短文）。5と0のつく日に出す。
    mugi = None
    if hotel and 宿の型(target_date) == "むぎ":
        旅の得 = お得日.旅(target_date)
        使える = [c for c in 宿.クーポンを読む()
                  if str(c.get("いつ", "いつでも")) != "5と0のつく日" or 旅の得]
        if 使える:
            mugi = {
                "お得日": 旅の得,
                "クーポン": 使える,
                "うたい文句": next((c.get("うたい文句") for c in 使える if c.get("うたい文句")), ""),
                "期限": next((c.get("期限") for c in 使える if c.get("期限")), None),
            }
            hotel = None   # この日は宿の一覧を出さない
            print(f"  → 今日は5と0のつく日なので、むぎ型（クーポン {len(使える)} 本）に差し替えます")
        else:
            print("::warning::5と0のつく日ですが、使えるクーポンがありません。9選型で出します。")

    model = pick_model(api_key)
    prompt = build_prompt(
        board, neta, articles, works, recent_texts(entries), target_date, needed, filled,
        hotel=hotel,
        hotel_hour=HOTEL_HOUR if (hotel or mugi) else None,
        mugi=mugi,
    )
    posts = generate(api_key, model, prompt, [hour for hour, *_ in needed])

    by_hour = {int(p["hour"]): p for p in posts}
    new_lines = []
    # その日すでにキューに入っている投稿の出典も数に入れる。
    # YU さんが手で入れたものは例外なので、ここには含めない（note に「指示」と書く）。
    出典の枠: dict[str, int] = {}
    for h, e in filled.items():
        if "指示" in str(e.get("note", "")):
            continue
        for u in source_urls(e.get("text", ""), e.get("thread") or []):
            出典の枠[u] = h

    for hour, *_ in needed:
        try:
            post = by_hour.get(hour)
            if not post:
                # 3回聞いても返らなかった枠。ここで止めると他の枠まで捨てることになる
                print(f"::warning::{hour}:00 は作れませんでした。この枠は空のままにします。")
                continue
            text = (post.get("text") or "").strip()
            if not text:
                落とす(f"{hour}:00 の本文が空です。")
            # 検索で引っかかるように、本文の最後に語を足す。
            # AI が自分で書いていたら二重にしない。PR の枠には付けない。
            if hour != HOTEL_HOUR and 検索語 not in text:
                text = text.rstrip() + "\n\n" + 検索語
            thread = [t.strip() for t in (post.get("thread") or []) if t and t.strip()]
            for part in [text, *thread]:
                if len(part) > 500:
                    落とす(f"{hour}:00 に 500 字を超える要素があります（{len(part)} 字）。")
            if mugi and hour == HOTEL_HOUR:
                if URL_IN_TEXT.search(text):
                    落とす(f"{hour}:00 の本文に URL が入っています。この枠では本文にリンクを書きません。")
                if not text.startswith(PR_MARKERS):
                    落とす(
                        f"{hour}:00 の本文が【PR】で始まっていません（先頭 20 字: {text[:20]!r}）。"
                        "ステマ規制のため、冒頭の表記は必須です。"
                    )
                if len(text) > 120:
                    落とす(f"{hour}:00 の本文が長すぎます（{len(text)} 字）。この枠は 40〜90 字です。")
                泊 = STAYED_VOICE.search(text)
                if 泊:
                    落とす(f"{hour}:00 の本文に「{泊.group(0)}」が入っています。泊まった体で書かないこと。")
                数 = むぎの数字(text, mugi)
                if 数:
                    落とす(
                        f"{hour}:00 の本文に「{数}」が入っています。"
                        "楽天のキャンペーンは予告なく変わるので、渡した数字以外は書きません。"
                        "（neta/宿_クーポン.jsonl の「うたい文句」に書いた数字だけ使えます）"
                    )
                thread = むぎの返信(mugi)

            if not hotel and not mugi and hour == HOTEL_HOUR and text.startswith(PR_MARKERS):
                # 宿の枠が立っていないのに PR 投稿が作られた。
                # リンクが付かないので成果にならず、表示だけが残る。
                落とす(
                    f"{hour}:00 が【PR】で始まっていますが、今日は紹介できる宿がありません"
                    f"（先頭 30 字: {text[:30]!r}）。"
                )

            if hotel and hour == HOTEL_HOUR:
                # AI に書かせるのは見出し1行だけ。宿の一覧・リンクはこちらで組み立てる。
                見出し = text.splitlines()[0].strip() if text else ""
                if not 見出し:
                    落とす(f"{hour}:00 の見出しが空です。")
                if URL_IN_TEXT.search(見出し):
                    落とす(f"{hour}:00 の見出しに URL が入っています。")
                if 見出し.startswith(PR_MARKERS):
                    落とす(
                        f"{hour}:00 の見出しが【PR】で始まっています。"
                        "1本目にはリンクを入れないので、PR は返信の先頭に付けます。"
                    )
                if "福井" not in 見出し:
                    落とす(f"{hour}:00 の見出しに「福井」が入っていません（{見出し!r}）。")
                if len(見出し) > 34:
                    落とす(f"{hour}:00 の見出しが長すぎます（{len(見出し)} 字）: {見出し!r}")
                泊 = STAYED_VOICE.search(見出し)
                if 泊:
                    落とす(
                        f"{hour}:00 の見出しに「{泊.group(0)}」が入っています。"
                        "この宿には泊まっていません。"
                    )
                text = 宿の本文(hotel["raw"], 見出し)
                thread = 宿の返信(hotel["raw"], target_date)

            # 同じ催しを 1 日に 2 本出していないかを、ここで機械的に確かめる。
            # 指示だけだと読み飛ばされる（9/24 ぶんで 3 組の重複が通った）。
            重なり = source_urls(text, thread) & set(出典の枠)
            if 重なり:
                # 重なった枠だけを落とす。ここで fail すると、その日の10本が
                # まるごと捨てられる（2026-09-24 に実際に起きて、9/25 が空になった）。
                # 1本の取りこぼしと、1日の全滅は釣り合わない。
                どこ = "、".join(f"{h}:00" for h in sorted(出典の枠[u] for u in 重なり))
                print(
                    f"::warning::{hour}:00 は {どこ} と同じ出来事なので入れません"
                    f"（出典 {sorted(重なり)[0]}）。この枠は空のままにします。"
                )
                continue
            for u in source_urls(text, thread):
                出典の枠[u] = hour

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
        except 枠を落とす as わけ:
            print(f"::warning::{hour}:00 は作れませんでした（{わけ}）。この枠は空のままにします。")
            continue

    if dry_run:
        print("\nDRY_RUN のため、キューには書き込みません。")
        return

    with QUEUE_PATH.open("a", encoding="utf-8") as handle:
        for line in new_lines:
            handle.write(line + "\n")
    print(f"\nキューに {len(new_lines)} 件追加しました。")


if __name__ == "__main__":
    main()
