"""日次レビュー — 投稿の数字を取り、何が効いたかを判定し、翌日の生成指示を 1 つだけ更新する。

マーケティング部・検証改善チームの実務。毎日 19:15 JST ごろ（compose の前）に動く。

流れ:
  1. state/posted.json と posts/queue.jsonl から、24 時間以上経った投稿を集める
  2. API で数字（閲覧・いいね・返信・リポスト等）を取り insights/metrics.jsonl に追記する
  3. 直近 7 日を集計し、書き出し・長さ・連投・枠ごとの傾向を出す
  4. 「今日の変更」を 1 つだけ決めて insights/learnings.md を書き換える
     （compose.py がこれを読み、翌日の生成に反映する）
  5. insights/daily/YYYY-MM-DD.md に検証ログを残す

判定のルール:
  - 1 日に動かすレバーは 1 つ。同時に複数変えると、何が効いたか分からなくなる
  - 測定済みが 6 本未満なら「データ不足・変更なし」
  - 同じレバーは 3 日続けて動かさない（効果が出る前に戻してしまうのを防ぐ）
  - 文体・禁止事項・事実の扱い（運用ボード）には触らない。変えるのは切り口・長さ・連投・話題の比重だけ

必要な環境変数:
  PLATFORM               "threads" または "x"
  THREADS_ACCESS_TOKEN   threads のとき必須
  X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET   x のとき必須
  ANTHROPIC_API_KEY      任意。あれば学びの文章を AI が書く。無ければ数字だけの機械的な文で書く
  ANTHROPIC_MODEL        任意
  DRY_RUN                "true" ならファイルを書かない
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "posts/queue.jsonl"
POSTED_PATH = ROOT / "state/posted.json"
INSIGHTS_DIR = ROOT / "insights"
METRICS_PATH = INSIGHTS_DIR / "metrics.jsonl"
CHANGES_PATH = INSIGHTS_DIR / "changes.jsonl"
LEARNINGS_PATH = INSIGHTS_DIR / "learnings.md"
DAILY_DIR = INSIGHTS_DIR / "daily"

PLATFORM = os.environ.get("PLATFORM", "threads").strip().lower()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"

MIN_AGE_HOURS = 24          # 投稿から 24 時間たったものだけ測る（数字が落ち着くのを待つ）
WINDOW_DAYS = 7             # 判定に使う期間
MIN_SAMPLES = 6             # これ未満なら変更しない
LEVER_COOLDOWN_DAYS = 3     # 同じレバーを続けて動かさない日数

# 動かしてよいレバー（運用ボードの文体・禁止事項・事実の扱いには触らない）
LEVERS = ("書き出し", "長さ", "連投", "話題の比重")

ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


def fail(message: str) -> None:
    print(f"::error::{message}", flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------- 読み込み

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def load_posted() -> dict[str, dict]:
    if not POSTED_PATH.exists():
        return {}
    data = json.loads(POSTED_PATH.read_text(encoding="utf-8") or "{}")
    return data.get("posted", {})


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return text.strip()[:40]


def opening_type(line: str) -> str:
    """書き出しの型を機械的に分類する（AI は使わない）。"""
    if line.endswith(("?", "？")):
        return "問いかけ"
    if any(ch.isdigit() for ch in line):
        return "数字あり"
    if len(line) <= 20:
        return "短い断言"
    return "説明文"


def length_bucket(n: int) -> str:
    if n <= 80:
        return "短（〜80字）"
    if n <= 150:
        return "中（81〜150字）"
    return "長（151字〜）"


def collect_targets(now: datetime) -> list[dict]:
    """測定対象（24h 以上・8 日以内に投稿されたもの）を集める。"""
    queue = {str(e.get("id")): e for e in load_jsonl(QUEUE_PATH) if e.get("id")}
    posted = load_posted()
    targets = []
    for item_id, record in posted.items():
        posted_at = parse_iso(record.get("posted_at", ""))
        post_id = record.get("post_id")
        if not posted_at or not post_id:
            continue
        age = now - posted_at
        if age < timedelta(hours=MIN_AGE_HOURS) or age > timedelta(days=WINDOW_DAYS + 1):
            continue
        entry = queue.get(item_id, {})
        text = entry.get("text", "")
        thread = entry.get("thread") or []
        local = posted_at.astimezone(JST)
        targets.append(
            {
                "id": item_id,
                "post_id": str(post_id),
                "posted_at": posted_at.isoformat(),
                "date": local.date().isoformat(),
                "hour": local.hour,
                "text_len": len(text),
                "has_thread": bool(thread),
                "first_line": first_line(text) if text else "",
                "opening": opening_type(first_line(text)) if text else "不明",
                "note": entry.get("note", ""),
                "permalink": record.get("permalink", ""),
            }
        )
    targets.sort(key=lambda t: t["posted_at"])
    return targets


# --------------------------------------------------------------------------- 計測 API

def fetch_threads_metrics(post_ids: list[str]) -> dict[str, dict]:
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        fail("THREADS_ACCESS_TOKEN が未設定です。")
    metrics = {}
    for post_id in post_ids:
        params = urllib.parse.urlencode(
            {"metric": "views,likes,replies,reposts,quotes", "access_token": token}
        )
        url = f"https://graph.threads.net/v1.0/{post_id}/insights?{params}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            warn(f"{post_id}: insights を取れませんでした ({exc.code}) {detail}")
            if exc.code in (400, 403) and "permission" in detail.lower():
                fail(
                    "insights の権限がありません。Meta の Threads アプリに threads_manage_insights を追加し、"
                    "アクセストークンを取り直してください。"
                )
            continue
        except Exception as exc:  # noqa: BLE001
            warn(f"{post_id}: 通信に失敗しました ({exc})")
            continue
        row = {}
        for item in payload.get("data", []):
            name = item.get("name")
            value = None
            if "total_value" in item:
                value = item["total_value"].get("value")
            elif item.get("values"):
                value = item["values"][0].get("value")
            if name is not None:
                row[name] = int(value or 0)
        metrics[post_id] = row
    return metrics


def fetch_x_metrics(post_ids: list[str]) -> dict[str, dict]:
    sys.path.insert(0, str(ROOT / "src"))
    from x_bot.client import _auth_header, load_credentials  # type: ignore

    credentials = load_credentials()
    metrics = {}
    # 1 リクエストで最大 100 件。Pay Per Use なので回数は最小にする
    for start in range(0, len(post_ids), 100):
        chunk = post_ids[start : start + 100]
        url = "https://api.x.com/2/tweets"
        params = {"ids": ",".join(chunk), "tweet.fields": "public_metrics"}
        request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", method="GET")
        request.add_header("Authorization", _auth_header("GET", url, credentials, params=params))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            warn(f"X API から数字を取れませんでした ({exc.code}) {detail}")
            continue
        except Exception as exc:  # noqa: BLE001
            warn(f"X API に接続できませんでした ({exc})")
            continue
        for tweet in payload.get("data", []):
            pm = tweet.get("public_metrics", {})
            metrics[str(tweet["id"])] = {
                "views": int(pm.get("impression_count", 0)),
                "likes": int(pm.get("like_count", 0)),
                "replies": int(pm.get("reply_count", 0)),
                "reposts": int(pm.get("retweet_count", 0)),
                "quotes": int(pm.get("quote_count", 0)),
                "bookmarks": int(pm.get("bookmark_count", 0)),
            }
    return metrics


def fetch_metrics(post_ids: list[str]) -> dict[str, dict]:
    if not post_ids:
        return {}
    if PLATFORM == "x":
        return fetch_x_metrics(post_ids)
    return fetch_threads_metrics(post_ids)


# --------------------------------------------------------------------------- 集計

def engagement(row: dict) -> int:
    return sum(int(row.get(k, 0)) for k in ("likes", "replies", "reposts", "quotes", "bookmarks"))


def summarize(rows: list[dict]) -> dict:
    """直近 7 日の測定値を、レバーごとに集計する。"""
    def group(key):
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            buckets[str(r[key])].append(r)
        out = {}
        def sort_key(kv):
            k = kv[0]
            return (0, int(k)) if k.isdigit() else (1, k)

        for name, items in sorted(buckets.items(), key=sort_key):
            views = [i["views"] for i in items]
            eng = [i["eng"] for i in items]
            out[name] = {
                "n": len(items),
                "views_avg": round(statistics.mean(views), 1) if views else 0,
                "eng_avg": round(statistics.mean(eng), 2) if eng else 0,
            }
        return out

    ranked = sorted(rows, key=lambda r: (r["views"], r["eng"]), reverse=True)
    return {
        "n": len(rows),
        "views_avg": round(statistics.mean(r["views"] for r in rows), 1) if rows else 0,
        "eng_avg": round(statistics.mean(r["eng"] for r in rows), 2) if rows else 0,
        "by_hour": group("hour"),
        "by_opening": group("opening"),
        "by_length": group("length_bucket"),
        "by_thread": group("has_thread"),
        "top": ranked[:3],
        "bottom": ranked[-3:][::-1] if len(ranked) >= 3 else [],
    }


def best_and_gap(table: dict) -> tuple[str | None, float]:
    """グループの中で最も閲覧が多いものと、最少との差（倍率）を返す。n<2 のグループは除く。"""
    usable = {k: v for k, v in table.items() if v["n"] >= 2}
    if len(usable) < 2:
        return None, 1.0
    ordered = sorted(usable.items(), key=lambda kv: kv[1]["views_avg"], reverse=True)
    top, low = ordered[0], ordered[-1]
    gap = (top[1]["views_avg"] / low[1]["views_avg"]) if low[1]["views_avg"] else 2.0
    return top[0], gap


def recent_changes(days: int) -> list[dict]:
    cutoff = datetime.now(JST) - timedelta(days=days)
    out = []
    for c in load_jsonl(CHANGES_PATH):
        when = parse_iso(c.get("at", ""))
        if when and when >= cutoff:
            out.append(c)
    return out


def decide(summary: dict) -> dict:
    """今日の変更を 1 つ決める。データが足りなければ変更なし。"""
    if summary["n"] < MIN_SAMPLES:
        return {
            "lever": None,
            "instruction": "変更なし（データ不足）",
            "reason": f"測定済みが {summary['n']} 本。{MIN_SAMPLES} 本そろうまで現状のまま続ける。",
        }
    cooling = {c["lever"] for c in recent_changes(LEVER_COOLDOWN_DAYS) if c.get("lever")}

    candidates = []
    best, gap = best_and_gap(summary["by_opening"])
    if best and gap >= 1.3:
        candidates.append(
            (gap, "書き出し", f"冒頭 1 行は「{best}」の型を基本にする（他の型は 3 本に 1 本まで）",
             f"書き出し「{best}」の平均閲覧が最少の型の {gap:.1f} 倍")
        )
    best, gap = best_and_gap(summary["by_length"])
    if best and gap >= 1.3:
        candidates.append(
            (gap, "長さ", f"本文の長さは「{best}」を基本にする",
             f"長さ「{best}」の平均閲覧が最少の帯の {gap:.1f} 倍")
        )
    best, gap = best_and_gap(summary["by_thread"])
    if best and gap >= 1.3:
        label = "連投（thread）を付ける" if best == "True" else "連投を付けず 1 本で完結させる"
        candidates.append((gap, "連投", label, f"連投{'あり' if best == 'True' else 'なし'}の平均閲覧が {gap:.1f} 倍"))
    best, gap = best_and_gap(summary["by_hour"])
    if best and gap >= 1.5:
        candidates.append(
            (gap, "話題の比重", f"{best}:00 の枠の話題・切り口を、他の枠でも 1 本は使う",
             f"{best}:00 の枠の平均閲覧が最少の枠の {gap:.1f} 倍")
        )

    candidates = [c for c in candidates if c[1] not in cooling]
    if not candidates:
        return {
            "lever": None,
            "instruction": "変更なし（差が小さい、または同じレバーを動かした直後）",
            "reason": "レバー間の差が 1.3 倍未満。いまの方針を続けて数字を積む。",
        }
    gap, lever, instruction, reason = max(candidates, key=lambda c: c[0])
    return {"lever": lever, "instruction": instruction, "reason": reason}


# --------------------------------------------------------------------------- 文章化

def anthropic_request(path: str, body: dict | None, api_key: str) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(ANTHROPIC_API + path, data=data, method="POST" if body else "GET")
    request.add_header("x-api-key", api_key)
    request.add_header("anthropic-version", ANTHROPIC_VERSION)
    request.add_header("content-type", "application/json")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_model(api_key: str) -> str:
    explicit = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if explicit:
        return explicit
    ids = [m["id"] for m in anthropic_request("/models?limit=100", None, api_key).get("data", [])]
    for keyword in ("sonnet", "haiku", "opus"):
        for model_id in ids:
            if keyword in model_id:
                return model_id
    return ids[0] if ids else ""


def write_learning_text(summary: dict, decision: dict, history: list[dict]) -> str:
    """「続けること」の 2〜3 行を書く。AI が使えなければ機械的な文で埋める。"""
    fallback = []
    for r in summary["top"][:2]:
        fallback.append(f"- 「{r['first_line'][:30]}」（{r['hour']}:00・閲覧 {r['views']}）のような切り口は続ける")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "\n".join(fallback) or "- （まだ材料がありません）"
    try:
        model = pick_model(api_key)
        table = json.dumps(
            {k: summary[k] for k in ("n", "views_avg", "eng_avg", "by_hour", "by_opening", "by_length", "by_thread")},
            ensure_ascii=False,
        )
        tops = "\n".join(f"- {r['hour']}:00 閲覧{r['views']} 反応{r['eng']}: {r['first_line']}" for r in summary["top"])
        lows = "\n".join(f"- {r['hour']}:00 閲覧{r['views']} 反応{r['eng']}: {r['first_line']}" for r in summary["bottom"])
        prompt = "\n".join(
            [
                "あなたは SNS 発信の検証チームです。以下は直近 7 日の投稿の数字です。",
                "「続けること」を 2〜3 行の箇条書きで書いてください。",
                "ルール: 数字に根拠があることだけ書く。推測や一般論は書かない。文体や禁止事項の変更は提案しない。",
                "1 行 60 字以内。前置き・見出し・説明は不要。箇条書き（- で始まる行）だけを返す。",
                "",
                "## 集計", table,
                "## 閲覧が多かった投稿", tops or "（なし）",
                "## 閲覧が少なかった投稿", lows or "（なし）",
                "## 今日決めた変更", f"{decision['lever'] or 'なし'}: {decision['instruction']}（{decision['reason']}）",
            ]
        )
        payload = anthropic_request(
            "/messages", {"model": model, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]}, api_key
        )
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text").strip()
        lines = [ln for ln in text.splitlines() if ln.strip().startswith("-")]
        return "\n".join(lines[:3]) if lines else "\n".join(fallback)
    except Exception as exc:  # noqa: BLE001
        warn(f"学びの文章を AI で書けませんでした ({exc})。機械的な文で代替します。")
        return "\n".join(fallback) or "- （まだ材料がありません）"


def render_table(title: str, table: dict, label_map=None) -> list[str]:
    lines = [f"### {title}", "| 区分 | 本数 | 平均閲覧 | 平均反応 |", "|---|---|---|---|"]
    for key, v in table.items():
        label = label_map(key) if label_map else key
        lines.append(f"| {label} | {v['n']} | {v['views_avg']} | {v['eng_avg']} |")
    return lines + [""]


def render_learnings(now: datetime, summary: dict, decision: dict, keep: str, history: list[dict]) -> str:
    active = [c for c in history if c.get("lever")][-3:]
    lines = [
        "# 検証チームからの指示（自動更新・毎日）",
        "",
        f"更新: {now.strftime('%Y-%m-%d %H:%M')} JST ／ 対象: 直近 {WINDOW_DAYS} 日・測定済み {summary['n']} 本 ／ 平均閲覧 {summary['views_avg']}・平均反応 {summary['eng_avg']}",
        "",
        "compose.py はこのファイルを読み、翌日の生成に反映する。運用ボードの文体・禁止事項・事実の扱いはこの指示より優先。",
        "",
        "## 今日の変更（1 つだけ）",
        f"- レバー: {decision['lever'] or 'なし'}",
        f"- 指示: {decision['instruction']}",
        f"- 根拠: {decision['reason']}",
        "",
        "## いま有効な指示（直近 3 件の変更。新しいものほど優先）",
    ]
    if active:
        for c in reversed(active):
            lines.append(f"- [{c['at'][:10]}] {c['lever']}: {c['instruction']}")
    else:
        lines.append("- （まだありません）")
    lines += ["", "## 続けること", keep or "- （まだ材料がありません）", ""]
    lines += render_table("枠ごと", summary["by_hour"], lambda k: f"{k}:00")
    lines += render_table("書き出しの型", summary["by_opening"])
    lines += render_table("長さ", summary["by_length"])
    lines += render_table("連投", summary["by_thread"], lambda k: "あり" if k == "True" else "なし")
    return "\n".join(lines).rstrip() + "\n"


def render_daily(now: datetime, measured: list[dict], summary: dict, decision: dict) -> str:
    lines = [
        f"# 検証ログ {now.date().isoformat()}",
        "",
        f"測定: {len(measured)} 本（新規）／ 直近 {WINDOW_DAYS} 日の測定済み {summary['n']} 本",
        "",
        "## 今日の変更",
        f"- {decision['lever'] or 'なし'}: {decision['instruction']}",
        f"- 根拠: {decision['reason']}",
        "",
        "## 今日測った投稿",
        "| 投稿日 | 枠 | 字数 | 連投 | 書き出し | 閲覧 | 反応 | 冒頭 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in measured:
        lines.append(
            f"| {r['date']} | {r['hour']}:00 | {r['text_len']} | {'○' if r['has_thread'] else '—'} | {r['opening']} | {r['views']} | {r['eng']} | {r['first_line'][:28]} |"
        )
    lines += ["", "## 閲覧 上位 3 本"]
    for r in summary["top"]:
        lines.append(f"- {r['date']} {r['hour']}:00 閲覧 {r['views']}・反応 {r['eng']}: {r['first_line'][:40]}")
    lines += ["", "## 閲覧 下位 3 本"]
    for r in summary["bottom"]:
        lines.append(f"- {r['date']} {r['hour']}:00 閲覧 {r['views']}・反応 {r['eng']}: {r['first_line'][:40]}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main

def main() -> None:
    now = datetime.now(JST)
    log(f"日次レビュー開始: {now.isoformat()} ／ platform={PLATFORM}")

    targets = collect_targets(now.astimezone(timezone.utc))
    log(f"測定対象: {len(targets)} 本")

    fetched = fetch_metrics([t["post_id"] for t in targets])
    measured = []
    for t in targets:
        row = fetched.get(t["post_id"])
        if row is None:
            continue
        record = dict(t)
        record.update(
            {
                "measured_at": now.isoformat(),
                "views": int(row.get("views", 0)),
                "likes": int(row.get("likes", 0)),
                "replies": int(row.get("replies", 0)),
                "reposts": int(row.get("reposts", 0)),
                "quotes": int(row.get("quotes", 0)),
                "bookmarks": int(row.get("bookmarks", 0)),
            }
        )
        record["eng"] = engagement(record)
        record["length_bucket"] = length_bucket(record["text_len"])
        measured.append(record)
    log(f"数字を取れた投稿: {len(measured)} 本")

    # 直近 7 日の判定には「各投稿の最新の測定値」を使う（同じ投稿を毎日測るので最新を採る）
    history_rows = load_jsonl(METRICS_PATH)
    latest: dict[str, dict] = {}
    for r in history_rows + measured:
        posted_at = parse_iso(r.get("posted_at", ""))
        if not posted_at or now.astimezone(timezone.utc) - posted_at > timedelta(days=WINDOW_DAYS + 1):
            continue
        latest[r["id"]] = r
    rows = list(latest.values())
    for r in rows:
        r.setdefault("length_bucket", length_bucket(int(r.get("text_len", 0))))
        r.setdefault("eng", engagement(r))
    summary = summarize(rows)
    decision = decide(summary)
    log(f"判定: {decision['lever'] or 'なし'} — {decision['instruction']}")

    history = load_jsonl(CHANGES_PATH)
    change = {"at": now.isoformat(), **decision, "n": summary["n"], "views_avg": summary["views_avg"]}
    keep = write_learning_text(summary, decision, history)

    learnings = render_learnings(now, summary, decision, keep, history + [change])
    daily = render_daily(now, measured, summary, decision)

    if DRY_RUN:
        log("\n--- learnings.md (DRY_RUN) ---\n" + learnings)
        log("\n--- daily (DRY_RUN) ---\n" + daily)
        return

    INSIGHTS_DIR.mkdir(exist_ok=True)
    DAILY_DIR.mkdir(exist_ok=True)
    with METRICS_PATH.open("a", encoding="utf-8") as handle:
        for r in measured:
            handle.write(json.dumps(r, ensure_ascii=False) + "\n")
    with CHANGES_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(change, ensure_ascii=False) + "\n")
    LEARNINGS_PATH.write_text(learnings, encoding="utf-8")
    (DAILY_DIR / f"{now.date().isoformat()}.md").write_text(daily, encoding="utf-8")
    log("insights/ を更新しました。")


if __name__ == "__main__":
    main()
