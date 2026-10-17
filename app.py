import streamlit as st
import os
import re
import base64
import datetime
from datetime import timezone, timedelta
import time
import matplotlib.pyplot as plt
from openai import OpenAI
import gspread

# ----------------------------------------------------
# 1. ページ初期設定
# ----------------------------------------------------
st.set_page_config(page_title="数学ゼミ議論シミュレータ", layout="wide")
st.title("数学ゼミ議論シミュレーション")

# セッション状態の初期化
if "messages_A" not in st.session_state:
    st.session_state.messages_A = []
if "messages_B" not in st.session_state:
    st.session_state.messages_B = []
if "current_turn" not in st.session_state:
    st.session_state.current_turn = 1
if "next_speaker" not in st.session_state:
    st.session_state.next_speaker = "A" # A: アキト, B: ハルタ
if "discussion_log" not in st.session_state:
    st.session_state.discussion_log = []
if "system_started" not in st.session_state:
    st.session_state.system_started = False
if "is_finished" not in st.session_state:
    st.session_state.is_finished = False
if "problem_text" not in st.session_state:
    st.session_state.problem_text = ""
if "has_image" not in st.session_state:
    st.session_state.has_image = False
if "is_geometry" not in st.session_state:
    st.session_state.is_geometry = False
if "image_content" not in st.session_state:
    st.session_state.image_content = None

# APIの設定
client = OpenAI(
    api_key=st.secrets.get("OPENAI_API_KEY"), 
)
MODEL = "gpt-6-sol"
SPREADSHEET_ID = "1_sRw5amkLY_-O0sj2VlIJklr05DSXJ3Lf8DIYxVSw5c"

# ----------------------------------------------------
# 2. Googleスプレッドシート接続
# ----------------------------------------------------
@st.cache_resource
def init_gspread():
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        return spreadsheet.get_worksheet(0)
    except Exception as e:
        st.error(f"❌ スプレッドシート接続失敗: {type(e).__name__} - {e}")
        return None

worksheet = init_gspread()

def log_to_sheet(speaker, content, usage=None, turn="-"):
    """スプレッドシート書き込み用ヘルパー関数"""
    if worksheet:
        try:
            jst = timezone(timedelta(hours=9), 'JST')
            timestamp = datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
            p_tok = usage.get("prompt_tokens", 0) if usage else 0
            c_tok = usage.get("completion_tokens", 0) if usage else 0
            t_tok = usage.get("total_tokens", 0) if usage else 0
            
            row = [timestamp, MODEL, turn, speaker, content, p_tok, c_tok, t_tok]
            worksheet.append_row(row)
        except Exception as e:
            st.sidebar.warning(f"スプレッドシート記録エラー: {e}")

def reset_session():
    """セッションの初期化関数"""
    keys = ["messages_A", "messages_B", "current_turn", "next_speaker", 
            "discussion_log", "system_started", "is_finished", 
            "problem_text", "has_image", "is_geometry", "image_content"]
    for key in keys:
        if key in st.session_state:
            del st.session_state[key]

# ----------------------------------------------------
# 3. プロンプト定義
# ----------------------------------------------------
MATH_FORMAT_INSTRUCTION = """
【数式記述の厳格ルール】
Markdownが数式を正しくレンダリングできるように、以下のルールを必ず守ってください。
1. 独立した行で数式を表示する場合（別行立て数式）は、必ず `$$` で囲んでください。
   （例） $$I = \\int_{0}^{\\pi/2} \\frac{5}{3\\sin x + 4\\cos x} \\,dx$$
   ※ `\\[ ... \\]` や単なる `[ ... ]` は絶対に使用しないでください。
2. 文章中に数式を挿入する場合（インライン数式）は、必ず単一の `$` で囲んでください。
   （例） 定数 $\\alpha$ を用いて表します。
   ※ `\\( ... \\)` は絶対に使用しないでください。
3. LaTeXのコマンド（`\\sin`, `\\cos`, `\\int` など）のバックスラッシュ（`\\`）がエスケープにより消えないよう、正確に記述してください。
"""

INSTRUCTION_A = """あなたはある数学ゼミに所属する理系大学生の「アキト」です。
友人の「ハルタ（高校三年生）」と一緒に、提示された数学の問題について話し合って解法を導き出してください。
ハルタが理解できるように、そしてこの議論を見ている読者が置いていかれないように、ゆっくりと段階を踏んで議論を進めてください。
ハルタは高校生なので、使える知識は高校生の範囲までです。大学数学の範囲を使ってはいけません。

【問題の情報】
{problem_description}

【あなたのキャラクター設定】
- 数学科の4年生。論理の飛躍を嫌い、定理や定義の厳密な適用を重んじる。
- 丁寧で落ち着いた、しかし等身大な大学生の口調（「〜だと思うよ」「〜はどうかな？」「ここで定義を確認しよう」など）。
- ハルタ（高校生）の等身大の理解力に寄り添い、難しい計算や式変形は一気に進めず、1ステップずつ丁寧に解説する。ハルタから質問されたら優しく噛み砕いて説明する。
- 結論を急がず、まずは問題の「前提条件の整理」や「図形の特徴の確認」など、簡単なところから段階的に対話を進める。
- ハルタはたまに間違えることがあるのでその都度修正してください。

【数式・定理の使用制限ルール（高校数学の範囲を厳守すること）】
あなたは「日本の高校数学（数I・A・II・B・III・C）」の範囲内にある定理・公式・記号のみを使用してください。大学数学の知識は【絶対に使用禁止】です。

特に以下の「大学数学の範囲」と「高校数学の範囲」の境界を厳守してください。NGになっているものは全て大学数学の範囲です。
1. 【対数の表記】
   - NG: 自然対数を `ln(x)` と書くこと。
   - OK: 必ず `log x` または `log_e x` と記述してください。

2. 【関数の制限】
   - NG: 三角関数の表記であるsec、csc、cot、逆三角関数（arctan, arcsin, arccos）、双曲線関数（sinh, cosh, tanh）、複素関数（オイラーの公式 e^(iθ) など）。
   - OK: arctan(x) を使いたくなったら、代わりに「tan θ = x となる角 θ をおいて…」と言い換えてください。

3. 【極限と微分の制限】
   - NG: ロピタルの定理、偏微分、全微分、テイラー展開（マクローリン展開）。
   - OK: 極限は、有理化や因数分解、高校の公式（lim x->0 sin(x)/x = 1 など）を丁寧に用いて計算してください。

4. 【代数・幾何の制限】
   - NG: 行列（行列式、固有値、一次変換などすべて）、外積（クロス積）。
   - OK: 2つの空間ベクトルに垂直なベクトルを求める際は、外積を使わず、内積=0の連立方程式を解いてください。

5. 【積分の制限】
   - NG: 重積分（二重積分など）、微分方程式。
   - OK: 積分の計算は、高校で習う置換積分・部分積分を用いて1変数のみで解いてください。

【ゼミ議論のルール】
1. 各発言は【200文字〜400文字】程度とし、簡潔でありながらも、説明が不親切にならないよう丁寧な記述を心がけてください。
2. **「一歩ずつ進むルール」**: 1回の発言で全体の解法を一気に説明してはいけません。「方針の決定」「1つの式変形」「図のプロット」など、1回につき1つのステップのみを扱い、ハルタの反応を待ってください。
3. 思考をビジュアルで整理したい場合、Pythonの「matplotlib」や「numpy」を用いた描写コードを積極的に書いてください。
   - コードは必ず ```python と ``` のブロックで囲んでください。
   - システムがコードを抽出し、あなたの代わりにグラフや図を即座に描画して議論の場に表示します。
   - 注意：描写エラーを防ぐため、図のタイトルや軸ラベルには日本語を使わず、必ず英語（または数式表記）で作成してください。
4. 完全に解法が明らかになり、ハルタも納得した段階で、高校生にも分かる丁寧な答案を作成してください。答案作成をしたら、『【議論終了】』という1行を追加してください。

{MATH_FORMAT_INSTRUCTION}
"""

INSTRUCTION_B = """あなたは高校三年生の「ハルタ」です。
理系大学生の先輩である「アキト」と一緒に、提示された数学の問題について話し合って解法を導き出してください。
この議論を見ている読者が置いていかれないよう、あなたが「一般的な学習者の代表」となって、分からない点があれば積極的に議論のスピードを落とす（ブレーキをかける）役割を担ってください。

【問題の情報】
{problem_description}

【あなたのキャラクター設定】
- 高校三年生。数学が苦手で公式すら間違えることがある。一見して解けない問題は分からない。
- 間違えたアイデアや計算をしてください。アキトからの修正があればそれに従ってください。
- 少しフランクで、問題解決に向けて熱心な高校生の口調（「なるほど！」「図に描いてみると分かりやすいかも」など）。
- アキトが少しでも難しい定理を使ったり、計算を省略したりしたときは、素直に「待って、そこがよく分からないんだけど…」と質問してください。

【数式・定理の使用制限ルール（高校数学の範囲を厳守すること）】
あなたは「日本の高校数学（数I・A・II・B・III・C）」の範囲内にある定理・公式・記号のみを使用してください。大学数学の知識は【絶対に使用禁止】です。

特に以下の「大学数学の範囲」と「高校数学の範囲」の境界を厳守してください。NGになっているものは全て大学数学の範囲です。
1. 【対数の表記】
   - NG: 自然対数を `ln(x)` と書くこと。
   - OK: 必ず `log x` または `log_e x` と記述してください。

2. 【関数の制限】
   - NG: 三角関数の表記であるsec、csc、cot、逆三角関数（arctan, arcsin, arccos）、双曲線関数（sinh, cosh, tanh）、複素関数（オイラーの公式 e^(iθ) など）。
   - OK: arctan(x) を使いたくなったら、代わりに「tan θ = x となる角 θ をおいて…」と言い換えてください。

3. 【極限と微分の制限】
   - NG: ロピタルの定理、偏微分、全微分、テイラー展開（マクローリン展開）。
   - OK: 極限は、有理化や因数分解、高校の公式（lim x->0 sin(x)/x = 1 など）を丁寧に用いて計算してください。

4. 【代数・幾何の制限】
   - NG: 行列（行列式、固有値、一次変換などすべて）、外積（クロス積）。
   - OK: 2つの空間ベクトルに垂直なベクトルを求める際は、外積を使わず、内積=0の連立方程式を解いてください。

5. 【積分の制限】
   - NG: 重積分（二重積分など）、微分方程式。
   - OK: 積分の計算は、高校で習う置換積分・部分積分を用いて1変数のみで解いてください。
   
【ゼミ議論のルール】
1. 各発言は【150文字〜350文字】程度。
2. 思考をビジュアルで整理したい場合、Pythonの「matplotlib」や「numpy」を用いた描写コード（```python ... ```）を積極的に書いてください（日本語ラベル禁止）。
3. 解法と答えに十分納得がいき、答案が完成したと確信できたら、アキトの答案作成をサポートし、完成後に『【議論終了】』という1行を追加してください。

{MATH_FORMAT_INSTRUCTION}
"""

# ----------------------------------------------------
# 4. 各種ユーティリティ
# ----------------------------------------------------
def clean_messages_for_api(messages, keep_image=False):
    cleaned = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, list):
            if keep_image:
                cleaned.append({"role": role, "content": content})
            else:
                text_only = "".join([item["text"] for item in content if item.get("type") == "text"])
                cleaned.append({"role": role, "content": text_only})
        else:
            cleaned.append({"role": role, "content": content})
    return cleaned

def format_math_formulas(text):
    if not text:
        return text

    # 基本的な括弧表記の置換
    text = text.replace(r'\[', '$$').replace(r'\]', '$$')
    text = text.replace(r'\(', '$').replace(r'\)', '$')

    lines = text.split('\n')
    fixed_lines = []

    for line in lines:
        stripped = line.strip()

        # パターンA: 行頭が '=' や '\qquad', '\frac' などのLaTeX数式で始まり、末尾にだけ '$$' がある場合
        # 例: =(t^2...)$$ や \qquad t=...$$
        if re.match(r'^[=\\].*?\$\$$', stripped) and not stripped.startswith('$$'):
            line = '$$ ' + stripped

        # パターンB: 行頭に '$$' があり、末尾に '$$' がない場合
        elif stripped.startswith('$$') and stripped.count('$$') == 1:
            line = line + ' $$'

        # パターンC: 行の途中に単独で '$$' があり、手前が数式らしい記述の場合
        # 例: = (式) $$ ここからは文章...
        elif '$$' in line and line.count('$$') % 2 != 0:
            # 行頭から最初の '$$' までの間に LaTeX 記号があるかチェック
            prefix, suffix = line.split('$$', 1)
            if any(char in prefix for char in ['\\', '^', '_', '{', '}', '=']):
                line = '$$ ' + prefix.strip() + ' $$ ' + suffix.strip()

        fixed_lines.append(line)

    text = '\n'.join(fixed_lines)

    # 全体を通して $ や $$ の整合性をチェック（奇数個なら末尾を補正して赤文字エラーを防ぐ）
    if text.count('$$') % 2 != 0:
        text += '$$'

    return text
    
def execute_generated_code(text):
    code_blocks = re.findall(r'```python\s*(.*?)\s*```', text, re.DOTALL)
    for code in code_blocks:
        st.caption("🎨 **エージェントが構築した図形:**")
        try:
            local_vars = {}
            fig = plt.figure()
            exec(code, globals(), local_vars)
            st.pyplot(fig)
            plt.close(fig)
        except Exception as e:
            st.warning(f"⚠️ 描画コードの実行中にエラーが発生しました: {e}")

# ----------------------------------------------------
# 5. API 呼び出し関数
# ----------------------------------------------------
def get_ai_response(messages, turn, has_image):
    keep_image = (not has_image) or (turn <= 2)
    api_messages = clean_messages_for_api(messages, keep_image=keep_image)

    retries = 3
    while retries > 0:
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                max_completion_tokens=2048
            )
            res_text = response.choices[0].message.content.strip()

            usage_info = {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0
            }
            if hasattr(response, 'usage') and response.usage:
                usage_info["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0)
                usage_info["completion_tokens"] = getattr(response.usage, "completion_tokens", 0)
                usage_info["total_tokens"] = getattr(response.usage, "total_tokens", 0)

            if res_text:
                return res_text, usage_info

        except Exception as e:
            time.sleep(3)
            retries -= 1

    return None, None

# ----------------------------------------------------
# 6. UIの構築
# ----------------------------------------------------
if not st.session_state.system_started:
    # ── 入力設定画面 ──
    st.subheader("📐 ゼミ議論の条件設定")
    
    with st.form("input_form"):
        choice = st.radio("数学問題の入力形式を選択してください", ("テキスト直接入力", "PNG画像をアップロード"))
        uploaded_file = st.file_uploader("問題を写したPNG画像（画像入力時のみ有効）", type=["png"])
        problem_input_text = st.text_area("考えて欲しい数学の問題を入力してください（テキスト入力時のみ有効）:", height=150)
        
        # 図形問題かどうかの選択
        is_geom = st.checkbox("📐 この問題は図形問題ですか？（エージェントに図形描画を指示します）", value=False)
        
        start_btn = st.form_submit_button("ゼミ議論を開始する")

    if start_btn:
        if choice == "テキスト直接入力" and not problem_input_text.strip():
            st.error("❌ 問題文テキストを入力してください。")
        elif choice == "PNG画像をアップロード" and uploaded_file is None:
            st.error("❌ 問題画像をアップロードしてください。")
        else:
            st.session_state.has_image = (choice == "PNG画像をアップロード")
            st.session_state.is_geometry = is_geom
            
            if st.session_state.has_image:
                bytes_data = uploaded_file.read()
                image_base64 = base64.b64encode(bytes_data).decode('utf-8')
                st.session_state.image_content = {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"}
                }
                st.session_state.problem_text = "[アップロード画像の問題]"
            else:
                st.session_state.image_content = None
                st.session_state.problem_text = problem_input_text

            problem_desc = f"提示された画像の問題" if st.session_state.has_image else f"提示された問題: {st.session_state.problem_text}"
            
            inst_A_formatted = INSTRUCTION_A.replace("{problem_description}", problem_desc).replace("{MATH_FORMAT_INSTRUCTION}", MATH_FORMAT_INSTRUCTION)
            inst_B_formatted = INSTRUCTION_B.replace("{problem_description}", problem_desc).replace("{MATH_FORMAT_INSTRUCTION}", MATH_FORMAT_INSTRUCTION)

            # 図形問題の場合の追加指示
            geom_instruction = ""
            if st.session_state.is_geometry:
                geom_instruction = "\n【重要】この問題は図形問題です。議論の最初に、問題の設定を図形として視覚化するため、まずはmatplotlibを使用したPythonコードを書いて図形を描画してください。"

            start_prompt_text_A = f"こちらの数学の問題を一緒に解き明かしてください。アキト、まずは君から問題の整理をし、議論をスタートして。{geom_instruction}\n\n【解くべき問題】\n{problem_desc}"
            start_prompt_text_B = f"こちらの数学の問題を一緒に解き明かしてください。\n\n【解くべき問題】\n{problem_desc}"

            # 履歴の初期化
            if st.session_state.has_image:
                st.session_state.messages_A = [
                    {"role": "system", "content": inst_A_formatted},
                    {"role": "user", "content": [{"type": "text", "text": start_prompt_text_A}, st.session_state.image_content]}
                ]
                st.session_state.messages_B = [
                    {"role": "system", "content": inst_B_formatted},
                    {"role": "user", "content": [{"type": "text", "text": start_prompt_text_B}, st.session_state.image_content]}
                ]
            else:
                st.session_state.messages_A = [
                    {"role": "system", "content": inst_A_formatted},
                    {"role": "user", "content": start_prompt_text_A}
                ]
                st.session_state.messages_B = [
                    {"role": "system", "content": inst_B_formatted},
                    {"role": "user", "content": start_prompt_text_B}
                ]

            # スプレッドシートに記録（問題開始ログ＆入力問題内容）
            log_to_sheet("SYSTEM", "========== [新規ゼミ議論 開始] ==========")
            log_to_sheet("USER_PROBLEM", f"[図形問題フラグ: {is_geom}] {st.session_state.problem_text}")

            st.session_state.system_started = True
            st.session_state.is_finished = False
            st.session_state.next_speaker = "A"
            st.rerun()

else:
    # ── 議論中画面 ──
    col_main, col_side = st.columns([3, 1])
    
    with col_side:
        st.subheader("⚙️ 設定情報")
        st.write(f"**現在のターン数:** {st.session_state.current_turn}")
        st.write(f"**次の発言予定:** {'アキト (数学科4年)' if st.session_state.next_speaker == 'A' else 'ハルタ (高校3年)'}")
        if st.session_state.is_geometry:
            st.info("📐 図形問題モード: ON")
        if st.session_state.has_image:
            st.info("🖼️ 画像問題を読み込んでいます")
        else:
            st.text_area("解いている問題:", value=st.session_state.problem_text, disabled=True, height=150)
            
        if st.button("セッションを強制リセット"):
            reset_session()
            st.rerun()

    with col_main:
        st.subheader("💬 議論ログ")
        
        # チャットログ表示
        for log in st.session_state.discussion_log:
            with st.chat_message(log["role"]):
                st.markdown(f"**{log['speaker']}**")
                st.markdown(log["content"])
                if "code_text" in log:
                    execute_generated_code(log["code_text"])
                if log.get("usage"):
                    st.caption(f"📊 Prompt: {log['usage']['prompt_tokens']} | Completion: {log['usage']['completion_tokens']} | Total: {log['usage']['total_tokens']}")
        
        st.divider()

        # 議論終了時の処理
        if st.session_state.is_finished:
            st.balloons()
            st.success("🤝 議論が完了しました！お疲れ様でした。")
            if st.button("🔄 次の新しい問題を解く"):
                reset_session()
                st.rerun()
        else:
            # 進行および介入フォーム
            with st.form("control_form", clear_on_submit=True):
                target_agent = st.radio(
                    "介入時の質問・意見の宛先:",
                    ("おまかせ (交互に進行)", "アキトに質問/発言させる", "ハルタに質問/発言させる"),
                    horizontal=True
                )
                user_intervention = st.text_input("💬 介入：指示や質問（空欄ならそのまま進行）")
                submitted = st.form_submit_button("➡️ 次のターンへ進む")
                
            if submitted:
                # ユーザー介入がある場合の処理
                if user_intervention.strip():
                    # 宛先による話者の明示
                    recipient_prefix = ""
                    if target_agent == "アキトに質問/発言させる":
                        recipient_prefix = "【アキト先輩への質問・意見】: "
                        st.session_state.next_speaker = "A"
                    elif target_agent == "ハルタに質問/発言させる":
                        recipient_prefix = "【ハルタへの質問・意見】: "
                        st.session_state.next_speaker = "B"
                    else:
                        recipient_prefix = "【ユーザーからの質問・意見】: "

                    intervention_text = f"{recipient_prefix}\n{user_intervention}\n\nこの意見も踏まえて、議論を続けてください。"
                    
                    st.session_state.messages_A.append({"role": "user", "content": intervention_text})
                    st.session_state.messages_B.append({"role": "user", "content": intervention_text})
                    st.session_state.discussion_log.append({
                        "role": "user",
                        "speaker": f"ユーザー（あなた） -> {target_agent.replace('に質問/発言させる', '')}",
                        "content": user_intervention,
                        "usage": None
                    })
                    
                    # ユーザー介入をスプレッドシートに記録
                    log_to_sheet(f"USER_INTERVENTION (To: {target_agent})", user_intervention, turn=st.session_state.current_turn)

                # キャラクターの発話生成
                if st.session_state.next_speaker == "A":
                    speaker = "アキト (数学科4年)"
                    role_icon = "assistant"
                    with st.spinner("🧑‍💻 アキトが思索中..."):
                        response_text, usage = get_ai_response(st.session_state.messages_A, st.session_state.current_turn, st.session_state.has_image)
                        if response_text:
                            response_text = format_math_formulas(response_text)
                            st.session_state.messages_A.append({"role": "assistant", "content": response_text})
                            st.session_state.messages_B.append({"role": "user", "content": response_text})
                            # 次はBへ交代
                            st.session_state.next_speaker = "B"
                else:
                    speaker = "ハルタ (高校3年)"
                    role_icon = "assistant"
                    with st.spinner("👨‍💻 ハルタが思索中..."):
                        response_text, usage = get_ai_response(st.session_state.messages_B, st.session_state.current_turn, st.session_state.has_image)
                        if response_text:
                            response_text = format_math_formulas(response_text)
                            st.session_state.messages_B.append({"role": "assistant", "content": response_text})
                            st.session_state.messages_A.append({"role": "user", "content": response_text})
                            # 次はAへ交代
                            st.session_state.next_speaker = "A"
                
                # 発話のログ・スプレッドシート保存
                if response_text:
                    st.session_state.discussion_log.append({
                        "role": role_icon,
                        "speaker": speaker,
                        "content": response_text,
                        "code_text": response_text,
                        "usage": usage
                    })
                    
                    log_to_sheet(speaker, response_text, usage=usage, turn=st.session_state.current_turn)
                    st.session_state.current_turn += 1
                    
                    # 議論終了判定
                    if "【議論終了】" in response_text:
                        st.session_state.is_finished = True
                    
                    st.rerun()
