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
import re
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
    # 種別は設備の項目では分からないので、neta/宿_手動.jsonl に手で書いたものを見る。
    {"名": "一棟まるごと借りたい", "種類": "種別", "値": ["一棟貸し"], "問い": "一棟まるごと借りられる宿"},
    {"名": "グランピングしたい", "種類": "種別", "値": ["グランピング"], "問い": "グランピングができる宿"},
    {"名": "泊まる場所ごと非日常", "種類": "種別", "値": ["一棟貸し", "グランピング"],
     "問い": "一棟貸し・グランピングの宿"},
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
    if 種類 == "種別":
        種 = str(宿.get("種別") or "")
        return 種 if 種 in (切り口.get("値") or []) else None
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


# ---------------------------------------------------------------
# まとめの型（2026-09-23 代表指示）
# ---------------------------------------------------------------
# 代表が見つけた、よく伸びている楽天トラベルの投稿の形に合わせる。
#   1本目 … 「【福井】◯◯な宿7選」＋ 宿名の一覧だけ。リンクを入れない
#   返信  … 冒頭に「PR」。宿ごとに一文とリンク。2軒ずつ
#   最後  … クーポンのリンク（あれば）
# 本文にリンクを入れると表示が落ちるので、リンクは返信に置く。
# 一覧は保存されやすく、返信まで読んだ人が踏む、という流れ。
# ---------------------------------------------------------------

# エリアごとの絵文字。土地が分かるものを選ぶ（飾りではなく手がかりにする）。
エリアの絵文字 = {
    "福井市": "🏙", "あわら・三国": "♨️", "坂井・丸岡": "🏯", "永平寺": "🛕",
    "勝山": "🦕", "大野": "⛰", "鯖江": "👓", "越前市": "🖌",
    "越前海岸": "🌊", "南越前": "🚃", "敦賀": "⚓", "美浜": "🏖",
    "若狭三方": "🦆", "小浜": "🐟", "おおい": "🌲", "高浜": "🏝",
}
丸数字 = "①②③④⑤⑥⑦⑧⑨⑩"

短縮の置き場 = Path("neta/宿_短縮.jsonl")
短縮のURL = (
    "https://raw.githubusercontent.com/yasu29fr/x-yu__fukui-bot/main/"
    "neta/%E5%AE%BF_%E7%9F%AD%E7%B8%AE.jsonl"
)


def _行を読む(置き場: Path, url: str) -> list[dict]:
    """jsonl を手元か https から読む。# で始まる行は飛ばす。"""
    if 置き場.exists():
        text = 置き場.read_text(encoding="utf-8")
    else:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "compose"})
            with urllib.request.urlopen(req, timeout=30) as res:
                text = res.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError):
            return []
    出 = []
    for 行 in text.splitlines():
        行 = 行.strip()
        if not 行 or 行.startswith("#"):
            continue
        try:
            出.append(json.loads(行))
        except json.JSONDecodeError:
            continue
    return 出


def 短縮を読む() -> dict[int, str]:
    """施設番号 → 短縮URL。

    楽天アフィリエイトの短縮URLは管理画面でしか作れないので、ここは自動で増えない。
    代表が作ったものを neta/宿_短縮.jsonl に入れる。
    形: {"番号":2128,"名":"アパホテル〈福井片町〉","url":"https://a.r10.to/xxxxxx"}
    「名」は人が見て確かめるためのもので、突き合わせには使わない（番号で引く）。
    無い宿は、これまで通り長いリンクを使う。混ざっていてもかまわない。
    """
    出, 見たURL = {}, {}
    きょう = date.today()
    for x in _行を読む(短縮の置き場, 短縮のURL):
        try:
            番 = int(x.get("番号"))
        except (TypeError, ValueError):
            continue
        u = str(x.get("url") or "").strip()
        if not 番 or not u.startswith("https://a.r10.to/"):
            continue
        # 同じリンクが2軒に付いていたら、どちらかが間違い。両方とも使わない。
        if u in 見たURL:
            print(f"::warning::短縮URL {u} が {見たURL[u]} と {番} の両方に付いています。"
                  "どちらか間違っているので、この2軒は長いリンクを使います。")
            出.pop(見たURL[u], None)
            continue
        # 発行から10年でアフィリエイト機能が切れる（楽天の案内）。
        # 切れても見た目は変わらず報酬だけ入らなくなるので、先に気づけるようにする。
        作 = str(x.get("作った日") or "")
        if len(作) == 10:
            try:
                d = date(int(作[:4]), int(作[5:7]), int(作[8:10]))
                のこり = (d.replace(year=d.year + 10) - きょう).days
                if のこり < 180:
                    print(f"::warning::短縮URL {u}（{x.get('名')}）の期限まで {のこり} 日です。"
                          "作り直してください。")
            except ValueError:
                pass
        見たURL[u] = 番
        出[番] = u
    return 出


def 宿のリンク先(宿: dict, 短縮: dict[int, str] | None = None) -> str:
    """その宿に貼るURL。短縮があればそちら、無ければ長いほう。"""
    if 短縮:
        try:
            u = 短縮.get(int(宿.get("番号")))
        except (TypeError, ValueError):
            u = None
        if u:
            return u
    return str(宿.get("url") or "")


クーポンの置き場 = Path("neta/宿_クーポン.jsonl")
クーポンのURL = (
    "https://raw.githubusercontent.com/yasu29fr/x-yu__fukui-bot/main/"
    "neta/%E5%AE%BF_%E3%82%AF%E3%83%BC%E3%83%9D%E3%83%B3.jsonl"
)


def 市町(宿: dict) -> str:
    """住所から市町名だけ取り出す。取れなければエリア名。

    「丹生郡越前町」のような郡つきは、町だけにする（読む人に要るのは町名）。
    """
    住 = str(宿.get("住所") or "")
    m = re.search(r"福井県(?:[^\d０-９]*?郡)?([^\d０-９]+?[市町村])", 住)
    return m.group(1) if m else str(宿.get("エリア") or "福井")


# 宿名のうしろに付く運営会社の名前。一覧に並べると読みにくいので外す。
# 宿そのものの名前は変えない（別の宿と取り違えないため、前半はそのまま）。
運営の名 = re.compile(r"[（(][^（）()]*(?:グループ|ホテルズ|ＢＢＨ|チェーン|旧[：:])[^（）()]*[）)]\s*$")


def 見せる名(宿: dict) -> str:
    """一覧や返信に出す宿の名前。"""
    名 = str(宿.get("名") or "").strip()
    名 = 運営の名.sub("", 名).strip()
    return 名 or str(宿.get("名") or "")


def 一文(宿: dict, 切り口: dict | None = None, 上限: int = 42) -> str:
    """楽天トラベルに載っている紹介文から、その宿の一文を作る。

    宿が自分で書いた文なので、こちらで言い換えない。切って並べるだけ。
    今日の切り口に触れている一節があれば、そこを選ぶ。
    「サウナがある宿」の紹介で WOWOW の話が出てくると、読む人が困るため。
    """
    s = str(宿.get("特色") or "").strip()
    s = re.sub(r"[■★☆◆▼▲●○【】\[\]]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" 　-・/")
    if not s:
        return ""
    # 「。」「！」で区切って、節ごとに見る
    節 = [x.strip(" 　、・") for x in re.split(r"(?<=[。！])", s) if x.strip(" 　、・")]
    語 = [str(w) for w in ((切り口 or {}).get("探す語") or [])]
    選 = next((x for x in 節 if any(w in x for w in 語)), None) if 語 else None
    if 選 is None:
        # 紹介文が今日の切り口に触れていないとき。
        # 「サウナがある宿」に WOWOW の話を出すと読む人が困るので、
        # 当てはまった中身（館内設備など）をそのまま一文にする。
        事実 = str(宿.get("この切り口の事実") or "").strip()
        if 事実:
            # 事実だけだと「浴衣」の一語が7軒ぶん並んで、名簿のようになる。
            # 短いときは、宿の紹介文の頭を足して、宿ごとの違いが見えるようにする。
            もと = 節[0] if 節 else ""
            if len(事実) <= 14 and もと:
                足す = もと[: max(0, 上限 - len(事実) - 1)].strip(" 　、・").rstrip("。！")
                if len(足す) >= 8:
                    return f"{事実}。{足す}"
            return 事実[:上限].strip(" 　、・")
        選 = 節[0] if 節 else s
    # 短すぎるときは次の節も足す
    i = 節.index(選) if 選 in 節 else 0
    while len(選) < 18 and i + 1 < len(節) and len(選) + len(節[i + 1]) <= 上限:
        i += 1
        選 = 選 + 節[i]
    if len(選) <= 上限:
        return 選.rstrip("。！")
    切 = 選[:上限]
    区 = max(切.rfind("、"), 切.rfind(" "), 切.rfind("・"))
    return (切[:区] if 区 > 14 else 切).strip(" 　、・")


def クーポンを読む() -> list[dict]:
    """手で登録したクーポンのリンク。無ければ空。

    楽天アフィリエイトのキャンペーンのリンクは管理画面でしか作れないので、
    ここは自動で増えない。代表が作ったものを neta/宿_クーポン.jsonl に入れる。
    形: {"名":"ホテル・温泉宿のクーポン","url":"https://a.r10.to/xxxx","いつ":"5と0のつく日"}
        「いつ」は "5と0のつく日" か "いつでも"。
    """
    return [
        x for x in _行を読む(クーポンの置き場, クーポンのURL)
        if x.get("url") and x.get("名")
    ]


def 今日のまとめ(対象日: date, 宿たち: list[dict] | None = None,
              いくつ: int = 7) -> dict | None:
    """その日のまとめ投稿の材料を返す。

    一覧の形にするので、当てはまる宿が少ない切り口は飛ばす
    （比べる形の 今日の切り口 は 2 軒でよかったが、こちらは 5 軒要る）。
    """
    宿たち = 読む() if 宿たち is None else 宿たち
    if not 宿たち:
        return None
    日数 = (対象日 - 起点).days
    最低 = min(5, いくつ)
    for i in range(len(切り口たち)):
        き = 切り口たち[(日数 + i) % len(切り口たち)]
        合う = []
        for 宿 in sorted(宿たち, key=lambda x: str(x.get("番号"))):
            事実 = 当てはまる(き, 宿)
            if 事実:
                合う.append({**宿, "この切り口の事実": 事実})
        if len(合う) < 最低:
            continue
        ずらし = 日数 % len(合う)
        並べ直し = 合う[ずらし:] + 合う[:ずらし]
        # エリアがばらけるように、まず違うエリアから1軒ずつ拾う
        選ぶ, 見た = [], set()
        for 宿 in 並べ直し:
            if 宿.get("エリア") in 見た:
                continue
            選ぶ.append(宿)
            見た.add(宿.get("エリア"))
            if len(選ぶ) >= いくつ:
                break
        for 宿 in 並べ直し:  # 足りなければ同じエリアからも足す
            if len(選ぶ) >= いくつ:
                break
            if 宿 not in 選ぶ:
                選ぶ.append(宿)
        return {"切り口": き, "宿": 選ぶ}
    return None


def 一覧の行(まとめ: dict) -> list[str]:
    """本文に並べる一覧。リンクは入れない。"""
    出 = []
    for i, 宿 in enumerate(まとめ["宿"]):
        絵 = エリアの絵文字.get(str(宿.get("エリア")), "📍")
        出.append(f"{丸数字[i]} {絵} {見せる名(宿)}（{市町(宿)}）")
    return 出


def _とじる(s: str) -> str:
    """切った拍子に開いたままになった括弧を落とす。"""
    for 開, 閉 in (("「", "」"), ("（", "）"), ("(", ")"), ("【", "】")):
        while s.count(開) > s.count(閉):
            i = s.rfind(開)
            if i < 0:
                break
            s = (s[:i] + s[i + 1:]).strip(" 　、・")
    return s


def 返信の行(まとめ: dict) -> list[str]:
    """返信に並べる、宿ごとの一文とリンク。1軒で1つ。"""
    短縮 = 短縮を読む()
    出 = []
    for i, 宿 in enumerate(まとめ["宿"]):
        説 = _とじる(一文(宿, まとめ.get("切り口")))
        頭 = f"{丸数字[i]} {見せる名(宿)}（{市町(宿)}"
        頭 += f"。{説}）" if 説 else "）"
        出.append(f"{頭}\n{宿のリンク先(宿, 短縮)}")
    return 出
