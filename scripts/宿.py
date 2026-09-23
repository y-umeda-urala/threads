"""その日に紹介する宿と、比べる切り口を決める。3アカウントで同じ結果になる。

なぜ同じ結果になるのか
----------------------
  - 宿のリストは 1 つだけ（X のリポジトリの neta/宿.jsonl）。
    X は手元のファイルを読み、福井と yu は同じファイルを https で読む。
  - 選び方は日付から計算する。乱数も、どこまで使ったかの記録も使わない。

書き方の大前提
--------------
  **「ある」は書けるが「ない」は書けない。**
  項目に無いのは「設備が無い」ではなく「宿が登録していない」かもしれない。
  だから「◯◯がある宿」は作れるが、「この宿には◯◯がありません」とは書かない。
  例外は、宿自身が文章で「ございません」と書いている場合だけ（駐車場など）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

宿の置き場 = Path("neta/宿.jsonl")
宿のURL = (
    "https://raw.githubusercontent.com/yasu29fr/x-yu__fukui-bot/main/"
    "neta/%E5%AE%BF.jsonl"
)
起点 = date(2026, 1, 1)

# 比べる切り口。データで割れるものだけを置く。
# 「探す語」が 館内設備・部屋の備品・風呂 のどれかに入っていれば「あり」。
切り口たち = [
    {"名": "大浴場ある？", "探す語": ["大浴場"], "問い": "大浴場がある宿"},
    {"名": "露天風呂ある？", "探す語": ["露天風呂"], "問い": "露天風呂がある宿"},
    {"名": "サウナある？", "探す語": ["サウナ"], "問い": "サウナがある宿"},
    {"名": "温泉？", "探す語": ["温泉"], "問い": "温泉が引いてある宿"},
    {"名": "浴衣ある？", "探す語": ["浴衣"], "問い": "浴衣が置いてある宿"},
    {"名": "コインランドリーある？", "探す語": ["コインランドリー"], "問い": "コインランドリーがある宿"},
    {"名": "送迎ある？", "探す語": ["送迎"], "問い": "送迎がある宿"},
    {"名": "マッサージある？", "探す語": ["マッサージ"], "問い": "マッサージが頼める宿"},
    {"名": "チェックアウト何時？", "種類": "チェックアウト", "問い": "チェックアウトが遅い宿"},
    {"名": "夜遅く着いても入れる？", "種類": "最終チェックイン", "問い": "最終チェックインが遅い宿"},
    {"名": "駐車場は無料？", "種類": "駐車場無料", "問い": "駐車場が無料と書いてある宿"},
    {"名": "朝食はどこで食べる？", "種類": "朝食", "問い": "朝食の場所が分かる宿"},
]


def 読む() -> list[dict]:
    """宿のリストを読む。手元に無ければ https で取りに行く。"""
    if 宿の置き場.exists():
        text = 宿の置き場.read_text(encoding="utf-8")
    else:
        try:
            req = urllib.request.Request(宿のURL, headers={"User-Agent": "compose"})
            with urllib.request.urlopen(req, timeout=30) as res:
                text = res.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"::warning::宿のリストを読めませんでした（{exc}）。今日は宿の紹介をしません。")
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _設備(宿: dict) -> list[str]:
    return [*(宿.get("館内設備") or []), *(宿.get("部屋の備品") or []), *(宿.get("風呂") or [])]


def _分(t) -> int | None:
    """'11:00' を分に。'29:00'（翌朝5時）もそのまま数として扱う。"""
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def 当てはまる(切り口: dict, 宿: dict) -> str | None:
    """その宿がこの切り口に当てはまるなら、投稿に使える一言を返す。"""
    種類 = 切り口.get("種類")
    if 種類 is None:
        設備 = _設備(宿)
        当たり = [s for s in 設備 if any(w in s for w in 切り口["探す語"])]
        # 館内設備と風呂の両方に同じ語が入っていることがある（「サウナ、サウナ」）
        重複なし = list(dict.fromkeys(当たり))
        # 短い（＝設備名そのもの）ものより、説明が付いているほうが投稿の材料になる
        重複なし.sort(key=lambda x: -len(x))
        return "、".join(重複なし[:2]) if 重複なし else None
    if 種類 == "チェックアウト":
        分 = _分(宿.get("チェックアウト"))
        return f"チェックアウト {宿['チェックアウト']}" if 分 and 分 >= 11 * 60 else None
    if 種類 == "最終チェックイン":
        分 = _分(宿.get("最終チェックイン"))
        return f"最終チェックイン {宿['最終チェックイン']}" if 分 and 分 >= 24 * 60 else None
    if 種類 == "駐車場無料":
        p = 宿.get("駐車場") or ""
        return p.strip()[:60] if "無料" in p else None
    if 種類 == "朝食":
        場所 = 宿.get("朝食の場所") or []
        return "、".join(sorted(set(場所))[:3]) if 場所 else None
    return None


def 今日の切り口(対象日: date, 宿たち: list[dict] | None = None) -> dict | None:
    """その日の切り口と、当てはまる宿を返す。

    2 軒以上当てはまらない切り口は、比べる形にならないので飛ばす。
    """
    宿たち = 読む() if 宿たち is None else 宿たち
    if not 宿たち:
        return None
    日数 = (対象日 - 起点).days
    for i in range(len(切り口たち)):
        き = 切り口たち[(日数 + i) % len(切り口たち)]
        合う = []
        for 宿 in sorted(宿たち, key=lambda x: str(x.get("番号"))):
            一言 = 当てはまる(き, 宿)
            if 一言:
                合う.append({**宿, "この切り口の事実": 一言})
        if len(合う) < 2:
            continue
        # 同じ切り口でも、日によって並びをずらす
        ずらし = 日数 % len(合う)
        並べ直し = 合う[ずらし:] + 合う[:ずらし]
        # エリアがばらけるように、先頭から違うエリアを拾う
        選ぶ, 見たエリア = [], set()
        for 宿 in 並べ直し:
            if 宿.get("エリア") in 見たエリア:
                continue
            選ぶ.append(宿)
            見たエリア.add(宿.get("エリア"))
            if len(選ぶ) == 3:
                break
        if len(選ぶ) < 2:
            選ぶ = 並べ直し[:3]
        return {"切り口": き, "宿": 選ぶ}
    return None


def 投稿用にする(選んだ: dict) -> dict:
    """compose.py が扱う形に直す。"""
    き, 宿たち = 選んだ["切り口"], 選んだ["宿"]
    行 = []
    for 宿 in 宿たち:
        かけら = [f"{宿['名']}（{宿['エリア']}）", 宿["この切り口の事実"]]
        if 宿.get("最寄駅"):
            かけら.append(f"最寄り {宿['最寄駅']}")
        if 宿.get("評価"):
            かけら.append(f"楽天の評価 {宿['評価']}（{宿.get('レビュー数')}件）")
        行.append("・".join(str(x) for x in かけら if x))
    return {
        "name": き["名"],
        "memo": f"{き['問い']}を{len(宿たち)}軒。" + " ／ ".join(行),
        "links": [f"【PR】{宿['名']}\n{宿['url']}" for 宿 in 宿たち],
        "raw": 選んだ,
    }
