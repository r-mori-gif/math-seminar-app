# -*- coding: utf-8 -*-
"""
作図支援モジュール（数学ゼミ議論シミュレーション用）

学習者がキャンバスに図をリアルタイムで描き、
  1) 描き方の記録（いつ、何を描き、何を消したか）
  2) 無操作・描き直しなどからの「詰まり」の検知
  3) 図の内容が問題の条件と合っているかの確認（画像をAIに見せて判定）
を行い、その結果をアキト／ハルタに伝えて、ヒントを出させる。

既存のアプリ本体（app.py）には、数行の追加だけで組み込める。
"""
import base64
import copy
import hashlib
import io
import json
import re
import time
from collections import Counter

import streamlit as st
from PIL import Image

try:
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except Exception:                      # 未インストールでも本体が落ちないようにする
    st_canvas = None
    CANVAS_AVAILABLE = False

HAS_FRAGMENT = hasattr(st, "fragment")   # Streamlit 1.37 以降

# ----------------------------------------------------
# 設定値（ここを変えると、声をかけるタイミングを調整できる）
# ----------------------------------------------------
IDLE_NOT_STARTED_SEC = 45     # 何も描かないまま、この秒数たったら「描き始められない」とみなす
IDLE_STALLED_SEC = 30         # 描いた後、この秒数操作がなければ「手が止まった」とみなす
REDRAW_LIMIT = 3              # 前回の声かけ以降、この回数以上消したら「描き直しが多い」とみなす
REDRAW_IDLE_SEC = 8           # 描き直しの判定は、操作が止まってから（描いている最中には声をかけない）
ADVICE_COOLDOWN_SEC = 60      # 自動の声かけの最短間隔
MAX_AUTO_ADVICE = 5           # 1回の議論で、自動で声をかける最大回数
MONITOR_INTERVAL_SEC = 3      # 無操作を確認する間隔
CANVAS_W, CANVAS_H = 640, 420

MODES = {                     # 画面の表示名 : st_canvas の drawing_mode
    "線分": "line",
    "円": "circle",
    "点": "point",
    "手描き（文字・記号）": "freedraw",
    "四角形": "rect",
    "移動・変形": "transform",
}
TARGETS = {"おまかせ": None, "アキトに聞く": "A", "ハルタに聞く": "B"}

KIND_JA = {"line": "線分", "circle": "円", "rect": "四角形", "path": "手描き",
           "polygon": "多角形", "polyline": "折れ線", "ellipse": "楕円", "triangle": "三角形"}
TYPE_JA = {
    "not_started": "描き始められていない",
    "stalled": "手が止まっている",
    "redrawing": "描き直しが続いている",
    "missing_conditions": "問題の条件が図に反映されていない",
    "wrong_figure": "図の形や位置関係が問題と合っていない",
    "on_track": "順調に進んでいる",
}

# 作図支援モードのとき、アキト／ハルタの最初のプロンプトに足す文
LEARNER_DRAWS_INSTRUCTION_A = (
    "\n【重要】この問題は図形問題で、学習者が自分で図を描いて考えます。"
    "あなたは最初に図を描くコードを出さず、問題の条件の整理から議論を始めてください。"
    "議論の途中でシステムから「学習者の作図の観察」が届いたら、答えや解法は教えず、"
    "観察に基づく短いヒントを1つだけ述べてください。図を描くコードは、学習者が求めない限り出さないでください。"
)
LEARNER_DRAWS_INSTRUCTION_B = (
    "\n【重要】この問題は図形問題で、学習者が自分で図を描いて考えます。図を描くコードは出さないでください。"
    "システムから「学習者の作図の観察」が届いたら、答えは教えず、学習者に寄り添って"
    "短い問いかけやヒントを1つだけ述べてください。"
)

# ----------------------------------------------------
# セッション状態
# ----------------------------------------------------
STATE_DEFAULTS = {
    "learner_draws": False,          # 作図支援モードか
    "draw_events": [],               # 作図イベントの記録
    "draw_started_at": None,         # 作図パネルを最初に表示した時刻
    "draw_last_sig": "[]",           # 直前の図の署名（変化の検出用）
    "draw_last_count": 0,            # 直前の図形の数
    "draw_last_activity_at": None,   # 最後に図が変化した時刻
    "draw_image": None,              # 最新のキャンバス画像（RGBA配列）
    "draw_advice_request": None,     # 声かけの依頼（本体側で処理する）
    "draw_last_advice_at": 0.0,
    "draw_auto_count": 0,
    "draw_auto_enabled": True,
    "draw_removed_baseline": 0,      # 前回の声かけ時点での「消した回数」
}
STATE_KEYS = list(STATE_DEFAULTS) + ["draw_mode_ja", "draw_target_ja"]


def init_state():
    for k, v in STATE_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = copy.deepcopy(v)


def reset_state():
    """セッションの初期化時に呼ぶ。キャンバスも新しいものに切り替える。"""
    ver = st.session_state.get("draw_canvas_version", 0) + 1
    for k in STATE_KEYS:
        if k in st.session_state:
            del st.session_state[k]
    st.session_state["draw_canvas_version"] = ver


# ----------------------------------------------------
# 作図の記録と指標
# ----------------------------------------------------
def _norm_type(o):
    return str(o.get("type", "")).lower()      # Fabric.js のバージョンで大文字小文字が違う


def _kind_counts(objs):
    c = Counter(KIND_JA.get(_norm_type(o), _norm_type(o) or "不明") for o in objs)
    return dict(c)


def _signature(objs):
    rows = []
    for o in objs:
        try:
            rows.append([
                _norm_type(o),
                int(round(float(o.get("left", o.get("x", 0)) or 0))),
                int(round(float(o.get("top", o.get("y", 0)) or 0))),
                int(round(float(o.get("width", 0) or 0))),
                int(round(float(o.get("height", 0) or 0))),
                len(o.get("path", []) or o.get("points", []) or []),
            ])
        except Exception:
            rows.append([_norm_type(o)])
    return hashlib.md5(json.dumps(rows).encode()).hexdigest() if rows else "[]"


def track(res):
    """st_canvas の戻り値から、作図イベント（追加・削除・変更）を記録する。"""
    s = st.session_state
    now = time.time()
    if s.draw_started_at is None:
        s.draw_started_at = now
        s.draw_last_activity_at = now
    if res is None or getattr(res, "json_data", None) is None:
        return
    objs = res.json_data.get("objects", []) or []
    if getattr(res, "image_data", None) is not None:
        s.draw_image = res.image_data
    sig = _signature(objs)
    if sig == s.draw_last_sig:
        return
    n = len(objs)
    if n > s.draw_last_count:
        action = "add"
    elif n < s.draw_last_count:
        action = "clear" if n == 0 else "remove"
    else:
        action = "modify"
    s.draw_events.append({
        "t": round(now - s.draw_started_at, 1),
        "action": action,
        "n": n,
        "kinds": _kind_counts(objs),
    })
    s.draw_last_sig = sig
    s.draw_last_count = n
    s.draw_last_activity_at = now


def compute_metrics(now=None):
    s = st.session_state
    now = now or time.time()
    t0 = s.draw_started_at or now
    last = s.draw_last_activity_at or t0
    ev = s.draw_events
    adds = [e for e in ev if e["action"] == "add"]
    removed_total = len([e for e in ev if e["action"] in ("remove", "clear")])
    times = [e["t"] for e in ev]
    gaps = [b - a for a, b in zip([0.0] + times[:-1], times)]
    idle = now - last
    return {
        "elapsed_sec": round(now - t0, 1),
        "time_to_first_stroke_sec": adds[0]["t"] if adds else None,
        "idle_sec": round(idle, 1),
        "n_objects": s.draw_last_count,
        "kinds": ev[-1]["kinds"] if ev else {},
        "n_added": len(adds),
        "n_removed_total": removed_total,
        "n_removed_since_advice": removed_total - s.draw_removed_baseline,
        "longest_pause_sec": round(max(gaps + [idle]), 1),
        "recent_events": ev[-8:],
    }


def classify_by_rules(m):
    """時間と操作の回数だけで、詰まりの種類を決める（AIは使わない）。"""
    if m["n_objects"] == 0 and m["n_added"] == 0 and m["idle_sec"] >= IDLE_NOT_STARTED_SEC:
        return "not_started"
    if m["n_removed_since_advice"] >= REDRAW_LIMIT and m["idle_sec"] >= REDRAW_IDLE_SEC:
        return "redrawing"
    if m["n_objects"] > 0 and m["idle_sec"] >= IDLE_STALLED_SEC:
        return "stalled"
    if m["n_objects"] == 0 and m["n_added"] > 0 and m["idle_sec"] >= IDLE_STALLED_SEC:
        return "stalled"               # 描いたものを全部消して止まっている
    return "on_track"


# ----------------------------------------------------
# AIによる診断（図の内容と問題文の突き合わせ）
# ----------------------------------------------------
DIAG_SYSTEM = """あなたは、高校生の数学の作図を見守る観察者です。
問題文（または問題の画像）、学習者が今までに描いた図の画像、作図の記録が与えられます。
次のJSONだけを出力してください。説明や前置きは書かないでください。

{
  "stuck_type": "not_started | stalled | redrawing | missing_conditions | wrong_figure | on_track のどれか1つ",
  "observation": "図について観察できた事実を1〜2文で（日本語）",
  "missing_or_wrong": ["問題文の条件のうち、図に反映されていない・誤っているもの（なければ空の配列）"],
  "hint_focus": "次に学習者に気づかせたい点を1つ（答えや解法は書かない）",
  "confidence": 0.0〜1.0
}

判定の基準:
- missing_conditions: 点・辺・角・長さ・円など、問題文にある要素が図に足りない。
- wrong_figure: 要素はあるが、位置関係や形が問題と合わない（例：接点でない所に点がある）。
- stalled / redrawing / not_started: 図の内容より、作図の記録（無操作の時間、消した回数）が主な理由のとき。
- on_track: 今の図に問題がなく、続きを描けばよいとき。
注意:
- 手描きの文字や記号は読み取りが不確かです。確信がないときは断定せず、confidenceを下げてください。
- 問題の答えや解き方は、どの項目にも書かないでください。"""


def _png_data_url(arr):
    img = Image.fromarray(arr.astype("uint8"))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _parse_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _fallback_diag(rule_type, m):
    notes = {
        "not_started": "まだ何も描かれていない。",
        "stalled": "図は途中で、しばらく操作が止まっている。",
        "redrawing": "描いては消す操作が続いている。",
        "on_track": "作図は進んでいる。",
    }
    return {"stuck_type": rule_type, "observation": notes.get(rule_type, ""),
            "missing_or_wrong": [], "hint_focus": "", "confidence": 0.0, "_source": "rules"}


def diagnose(client, model, metrics, rule_type):
    s = st.session_state
    # 何も描かれていないときは、画像を見る必要がないので、AIは呼ばない
    if metrics["n_objects"] == 0 or s.draw_image is None:
        return _fallback_diag(rule_type if rule_type != "on_track" else "not_started", metrics)

    log_for_ai = {k: v for k, v in metrics.items() if k != "recent_events"}
    content = [{"type": "text",
                "text": f"【問題】\n{s.problem_text}\n\n【作図の記録】\n"
                        f"{json.dumps(log_for_ai, ensure_ascii=False)}\n"
                        f"【ルールによる暫定判定】{rule_type}"}]
    if s.get("has_image") and s.get("image_content"):
        content.append({"type": "text", "text": "（問題は次の画像です）"})
        content.append(s.image_content)
    content.append({"type": "text", "text": "次の画像が、学習者が今までに描いた図です。"})
    content.append({"type": "image_url", "image_url": {"url": _png_data_url(s.draw_image)}})
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": DIAG_SYSTEM},
                      {"role": "user", "content": content}],
            max_completion_tokens=800,
        )
        d = _parse_json(resp.choices[0].message.content)
        if not d or d.get("stuck_type") not in TYPE_JA:
            return _fallback_diag(rule_type, metrics)
        d["_source"] = "llm"
        u = getattr(resp, "usage", None)
        if u:
            d["_usage"] = {"prompt_tokens": getattr(u, "prompt_tokens", 0),
                           "completion_tokens": getattr(u, "completion_tokens", 0),
                           "total_tokens": getattr(u, "total_tokens", 0)}
        return d
    except Exception:
        return _fallback_diag(rule_type, metrics)


# ----------------------------------------------------
# アキト／ハルタへの伝え方
# ----------------------------------------------------
def choose_speaker(diag):
    """内容の指摘はアキト（教え手）、手が止まったときの声かけはハルタ（学び手）。"""
    if diag["stuck_type"] in ("missing_conditions", "wrong_figure", "on_track"):
        return "A"
    return "B"


def _kinds_text(kinds):
    return "、".join(f"{k}{v}個" for k, v in kinds.items()) if kinds else "なし"


def build_agent_message(diag, m):
    stage = "まだ何も描いていない" if m["n_objects"] == 0 else "作図の途中"
    lines = [
        "【学習者の作図の観察（システムが自動で取得した情報）】",
        f"- 状況: {TYPE_JA.get(diag['stuck_type'], diag['stuck_type'])}（{stage}）",
        f"- 作図を始めてから {m['elapsed_sec']:.0f} 秒、最後の操作から {m['idle_sec']:.0f} 秒",
        f"- 図の要素: {_kinds_text(m['kinds'])}",
    ]
    if diag.get("observation"):
        lines.append(f"- 観察: {diag['observation']}")
    if diag.get("missing_or_wrong"):
        lines.append("- 図に反映されていない／誤っているかもしれない点: "
                     + "、".join(str(x) for x in diag["missing_or_wrong"]))
    if diag.get("hint_focus"):
        lines.append(f"- 気づかせたい点: {diag['hint_focus']}")
    lines += ["",
              "この観察を踏まえ、学習者に向けて、答えや解法は教えずに、次の一歩につながる"
              "短いヒントまたは問いかけを1つだけ述べてください。図を描くコードは出さないでください。"
              "手書きの図は読み取りが不確かなことがあるので、断定せず、確認する言い方にしてください。"]
    return "\n".join(lines)


def _summary_for_log(diag, m):
    t = TYPE_JA.get(diag["stuck_type"], diag["stuck_type"])
    obs = f" {diag['observation']}" if diag.get("observation") else ""
    return f"✏️ 作図の観察：{t}。{obs}（無操作 {m['idle_sec']:.0f} 秒）"


def handle_advice_request(client, model, get_ai_response, format_math_formulas, log_to_sheet):
    """声かけの依頼があれば、診断して、アキトかハルタにヒントを出させる。"""
    s = st.session_state
    req = s.get("draw_advice_request")
    if not req:
        return
    s.draw_advice_request = None

    with st.spinner("✏️ 作図の状況を確認しています…"):
        m = compute_metrics()
        rule_type = req.get("rule_type") or classify_by_rules(m)
        diag = diagnose(client, model, m, rule_type)

    msg = build_agent_message(diag, m)
    target = req.get("target") or choose_speaker(diag)

    s.messages_A.append({"role": "user", "content": msg})
    s.messages_B.append({"role": "user", "content": msg})
    s.discussion_log.append({"role": "user", "speaker": "システム（作図の観察）",
                             "content": _summary_for_log(diag, m), "usage": diag.get("_usage")})
    log_to_sheet("DRAWING_OBSERVATION",
                 json.dumps({"source": req.get("source"), "target": target,
                             "diagnosis": {k: v for k, v in diag.items() if k != "_usage"},
                             "metrics": m}, ensure_ascii=False),
                 usage=diag.get("_usage"), turn=s.current_turn)

    if target == "A":
        speaker, mine, other, after = "アキト (数学科4年)", s.messages_A, s.messages_B, "B"
    else:
        speaker, mine, other, after = "ハルタ (高校3年)", s.messages_B, s.messages_A, "A"
    s.next_speaker = target

    with st.spinner(f"💬 {speaker.split(' ')[0]}が考えています…"):
        text, usage = get_ai_response(mine, s.current_turn, s.has_image)
    if text:
        text = format_math_formulas(text)
        mine.append({"role": "assistant", "content": text})
        other.append({"role": "user", "content": text})
        s.next_speaker = after
        s.discussion_log.append({"role": "assistant", "speaker": speaker,
                                 "content": text, "code_text": text, "usage": usage})
        log_to_sheet(speaker, text, usage=usage, turn=s.current_turn)
        s.current_turn += 1
        if "【議論終了】" in text:
            s.is_finished = True

    s.draw_last_advice_at = time.time()
    s.draw_removed_baseline = m["n_removed_total"]
    st.rerun()


# ----------------------------------------------------
# 画面（作図パネル）
# ----------------------------------------------------
def _request_advice(source, target=None, rule_type=None):
    st.session_state.draw_advice_request = {"source": source, "target": target, "rule_type": rule_type}
    st.rerun()


def _canvas_body():
    s = st.session_state
    left, right = st.columns([4, 1])
    with right:
        mode_ja = st.radio("道具", list(MODES), key="draw_mode_ja")
        st.radio("ヒントを聞く相手", list(TARGETS), key="draw_target_ja")
        if st.button("💡 ヒントをもらう", key="draw_hint_btn"):
            _request_advice("manual", target=TARGETS[st.session_state.draw_target_ja])
    with left:
        res = st_canvas(
            fill_color="rgba(46, 139, 110, 0.08)",
            stroke_width=3,
            stroke_color="#1E4D40",
            background_color="#FFFFFF",
            height=CANVAS_H, width=CANVAS_W,
            drawing_mode=MODES[mode_ja],
            update_streamlit=True,
            display_toolbar=True,
            key=f"learner_canvas_{s.get('draw_canvas_version', 0)}",
        )
    track(res)


def _monitor_body():
    s = st.session_state
    m = compute_metrics()
    c1, c2 = st.columns([3, 2])
    with c1:
        st.caption(f"⏱ 最後の操作から {m['idle_sec']:.0f} 秒　｜　図形 {m['n_objects']} 個"
                   f"（{_kinds_text(m['kinds'])}）　｜　消した回数 {m['n_removed_total']}")
    with c2:
        st.checkbox("止まったら自動で声をかける", key="draw_auto_enabled")

    if not (s.draw_auto_enabled and s.draw_advice_request is None):
        return
    if s.draw_auto_count >= MAX_AUTO_ADVICE:
        return
    if time.time() - s.draw_last_advice_at < ADVICE_COOLDOWN_SEC:
        return
    rtype = classify_by_rules(m)
    if rtype != "on_track":
        s.draw_auto_count += 1
        _request_advice("auto", rule_type=rtype)


if HAS_FRAGMENT:
    # 図を描くたびに、ページ全体ではなくこの部分だけが再実行される（議論ログの再描画を避ける）
    _canvas_fragment = st.fragment(_canvas_body)
    _monitor_fragment = st.fragment(run_every=MONITOR_INTERVAL_SEC)(_monitor_body)
else:
    _canvas_fragment = _canvas_body
    _monitor_fragment = _monitor_body


def render_drawing_panel():
    """議論画面に作図パネルを表示する。"""
    init_state()
    st.subheader("✏️ 作図パネル")
    if not CANVAS_AVAILABLE:
        st.error("streamlit-drawable-canvas が見つかりません。インストールしてください。")
        return
    st.caption("問題の条件を、自分の手で図にしてみましょう。迷ったら「ヒントをもらう」を押せます。"
               "点の名前（A, B, C…）は「手描き」で書いてください。")
    _canvas_fragment()
    _monitor_fragment()
    if not HAS_FRAGMENT:
        st.info("お使いのStreamlitは自動検知（fragment）に対応していないため、ヒントボタンのみ使えます。")
