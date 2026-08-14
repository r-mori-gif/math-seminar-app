import streamlit as st
import os
import re
import base64
import datetime
import time
import matplotlib.pyplot as plt
from openai import OpenAI
import gspread

# ----------------------------------------------------
# 1. ページ初期設定
# ----------------------------------------------------
st.set_page_config(page_title="数学ゼミ議論シミュレータ", layout="wide")
st.title("🎓 数学ゼミ議論シミュレーション")

# セッション状態の初期化
if "messages_A" not in st.session_state:
    st.session_state.messages_A = []
if "messages_B" not in st.session_state:
    st.session_state.messages_B = []
if "current_turn" not in st.session_state:
    st.session_state.current_turn = 1
if "discussion_log" not in st.session_state:
    st.session_state.discussion_log = []
if "system_started" not in st.session_state:
    st.session_state.system_started = False
if "problem_text" not in st.session_state:
    st.session_state.problem_text = ""
if "has_image" not in st.session_state:
    st.session_state.has_image = False
if "image_content" not in st.session_state:
    st.session_state.image_content = None

# APIの設定
openrouter_api_key = st.secrets.get("OPENROUTER_API_KEY")
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=openrouter_api_key,
)
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
SPREADSHEET_ID = "1_sRw5amkLY_-O0sj2VlIJklr05DSXJ3Lf8DIYxVSw5c"

# ----------------------------------------------------
# 2. Googleスプレッドシート接続 (改行コード修正版)
# ----------------------------------------------------
@st.cache_resource
def init_gspread():
    try:
        # Secrets から辞書形式でコピーを取得
        creds_dict = dict(st.secrets["gcp_service_account"])
        
        # 【重要】Streamlit Secrets 特有の改行コードエスケープ問題を解消
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        return spreadsheet.get_worksheet(0)
        
    except Exception as e:
        # 詳細なエラー情報を画面に表示する
        st.error(f"❌ スプレッドシート接続失敗: {type(e).__name__} - {e}")
        # デバッグ用にエラーの詳細なトレースバックをコンソール/ターミナルにも出力する
        import traceback
        traceback.print_exc()
        return None

worksheet = init_gspread()

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
   - 数値計算による確認では使ってはいけません。あくまで描写コードはユーザー側に分かりやすく伝えるためのツールです。
4. 完全に解法が明らかになり、ハルタも納得した段階で、高校生にも分かる丁寧な答案を作成してください。答案作成をしたら、『【議論終了】』という1行を追加してください。

{MATH_FORMAT_INSTRUCTION}
"""

INSTRUCTION_B = """あなたは高校三年生の「ハルタ」です。
理系大学生の先輩である「アキト」と一緒に、提示された数学の問題について話し合って解法を導き出してください。
この議論を見ている読者が置いていかれないよう、あなたが「一般的な学習者の代表」となって、分からない点があれば積極的に議論のスピードを落とす（ブレーキをかける）役割を担ってください。

【問題の情報】
{problem_description}

【あなたのキャラクター設定】
- 高校三年生。直感的で、幾何学的なアプローチや、視覚的なイメージ（グラフや図形）化を得意とするが、複雑な数式変形や厳密な論理展開は少し苦手。
- 少しフランクで、問題解決に向けて熱心な高校生の口調（「なるほど！」「図に描いてみると分かりやすいかも」「ちょっとプロットしてみるね」など）。
- アキトが少しでも難しい定理を使ったり、計算を省略したり、議論を急ぎすぎたりしたときは、**素直に「待って、そこがよく分からないんだけど…」「どうしてその式になるの？」と質問し、アキトに詳しく説明し直してもらってください。**
- 高校生の範囲外の概念（大学数学など）が出たときは、「それって高校で習う？」と素直に指摘してください。
- 問題に対するアプローチ方法に関して、いろいろなアイデアを出してください。そのアイデアは間違っているものでも出すようにしてください。合っているかどうかはアキトに任せてください。

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
1. 各発言は【150文字〜350文字】程度で、簡潔かつ等身大なやり取りをしてください。
2. **「置いていかれないルール」**: アキトの説明に対して、自分が完全に理解できるまでは次のステップに進まないでください。必要に応じて「直感的なイメージ」や「具体的な数字での具体例」をアキトに要求してください。
3. 思考をビジュアルで整理したい場合、Pythonの「matplotlib」や「numpy」を用いた描写コードを積極的に書いてください。
   - コードは必ず ```python と ``` のブロックで囲んでください。
   - システムがコードを抽出し、あなたの代わりにグラフや図を即座に描画して議論の場に表示します。
   - 注意：描写エラーを防ぐため、図のタイトルや軸ラベルには日本語を使わず、必ず英語（または数式表記）で作成してください。
   - 数値計算による確認では使ってはいけません。あくまで描写コードはユーザー側に分かりやすく伝えるためのツールです。
4. 解法と答えに十分納得がいき、答案が完成したと確信できたら、アキトの答案作成をサポートし、完成後に『【議論終了】』という1行を追加してください。

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
    text = text.replace(r'\[', '$$').replace(r'\]', '$$')
    text = text.replace(r'\(', '$').replace(r'\)', '$')
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
# 5. リトライ処理付き API 呼び出し関数（Colab版仕様）
# ----------------------------------------------------
def get_ai_response(messages, turn, has_image):
    keep_image = (not has_image) or (turn <= 2)
    api_messages = clean_messages_for_api(messages, keep_image=keep_image)

    retries = 5
    while retries > 0:
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                max_tokens=2048
            )
            res_text = response.choices[0].message.content.strip()

            usage_info = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
            if hasattr(response, 'usage') and response.usage:
                usage_info["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0)
                usage_info["completion_tokens"] = getattr(response.usage, "completion_tokens", 0)
                usage_info["total_tokens"] = getattr(response.usage, "total_tokens", 0)

            if res_text and "[APIエラー" not in res_text:
                return res_text, usage_info

            raise Exception("不適切な空の応答が返されました")

        except Exception as e:
            err_str = str(e)
            # 429 レート制限や一時的なエラーの処理
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "rate-limited" in err_str or "Rate limit" in err_str:
                wait_seconds = 15.0
                match = re.search(r"Please retry in ([\d\.]+)s", err_str)
                if match:
                    wait_seconds = float(match.group(1)) + 1.0

                st.warning(f"⚠️ API制限(429)検出。{wait_seconds:.1f} 秒待機して自動再試行します... (リトライ残り: {retries}回)")
                time.sleep(wait_seconds)
                retries -= 1
                continue
            else:
                st.warning(f"一時的エラー: {e}。5秒後に再試行します... (リトライ残り: {retries}回)")
                time.sleep(5)
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
        
        start_btn = st.form_submit_button("ゼミ議論を開始する")

    if start_btn:
        # 入力チェック
        if choice == "テキスト直接入力" and not problem_input_text.strip():
            st.error("❌ 問題文テキストを入力してください。")
        elif choice == "PNG画像をアップロード" and uploaded_file is None:
            st.error("❌ 問題画像をアップロードしてください。")
        else:
            # セッション状態への書き込み
            st.session_state.has_image = (choice == "PNG画像をアップロード")
            
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

            # プロンプトの差し替えと組み立て
            problem_desc = f"提示された画像の問題" if st.session_state.has_image else f"提示された問題: {st.session_state.problem_text}"
            
            inst_A_formatted = INSTRUCTION_A.replace("{problem_description}", problem_desc).replace("{MATH_FORMAT_INSTRUCTION}", MATH_FORMAT_INSTRUCTION)
            inst_B_formatted = INSTRUCTION_B.replace("{problem_description}", problem_desc).replace("{MATH_FORMAT_INSTRUCTION}", MATH_FORMAT_INSTRUCTION)

            # 初回発言に問題文を明示的に付与（空白対策）
            start_prompt_text_A = f"こちらの数学の問題を一緒に解き明かしてください。アキト、まずは君から最初の気づきを話して議論をスタートして。\n\n【解くべき問題】\n{problem_desc}"
            start_prompt_text_B = f"こちらの数学の問題を一緒に解き明かしてください。\n\n【解くべき問題】\n{problem_desc}"

            # 履歴の初期化
            if st.session_state.has_image:
                st.session_state.messages_A = [
                    {"role": "system", "content": inst_A_formatted},
                    {"role": "user", "content": [
                        {"type": "text", "text": start_prompt_text_A},
                        st.session_state.image_content
                    ]}
                ]
                st.session_state.messages_B = [
                    {"role": "system", "content": inst_B_formatted},
                    {"role": "user", "content": [
                        {"type": "text", "text": start_prompt_text_B},
                        st.session_state.image_content
                    ]}
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

            # スプレッドシートに区切り行追加
            if worksheet:
                try:
                    timestamp_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    divider = [timestamp_now, "SYSTEM", "-", "-", "========== [新規ゼミ議論 開始] ==========", "-", "-", "-"]
                    worksheet.append_row(divider)
                except Exception as e:
                    st.warning(f"スプレッドシートへの記録に失敗しました: {e}")

            st.session_state.system_started = True
            st.rerun()

else:
    # ── 議論中画面 ──
    col_main, col_side = st.columns([3, 1])
    
    with col_side:
        st.subheader("⚙️ 設定情報")
        st.write(f"**現在のターン数:** {st.session_state.current_turn} / 20")
        if st.session_state.has_image:
            st.info("🖼️ 画像問題を読み込んでいます")
        else:
            st.text_area("解いている問題:", value=st.session_state.problem_text, disabled=True, height=150)
            
        if st.button("セッションをリセット"):
            for key in ["messages_A", "messages_B", "current_turn", "discussion_log", "system_started", "problem_text", "has_image", "image_content"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    with col_main:
        st.subheader("💬 議論ログ")
        
        # チャットログ表示
        for log in st.session_state.discussion_log:
            role = log["role"]
            speaker_name = log["speaker"]
            content = log["content"]
            
            with st.chat_message(role):
                st.markdown(f"**{speaker_name}**")
                st.markdown(content)
                if "code_text" in log:
                    execute_generated_code(log["code_text"])
                if "usage" in log and log["usage"]:
                    st.caption(f"📊 Prompt: {log['usage']['prompt_tokens']} | Completion: {log['usage']['completion_tokens']} | Total: {log['usage']['total_tokens']}")
        
        st.divider()
        
        # 進行および介入フォーム
        with st.form("control_form", clear_on_submit=True):
            user_intervention = st.text_input("💬 介入：指示や質問（空欄ならそのまま進行）")
            submitted = st.form_submit_button("➡️ 次のターンへ進む")
            
        if submitted:
            # 1. ユーザー介入の追加
            if user_intervention.strip():
                intervention_text = f"【ユーザーからの質問・意見】:\n{user_intervention}\n\nこの意見も踏まえて、議論を続けてください。"
                st.session_state.messages_A.append({"role": "user", "content": intervention_text})
                st.session_state.messages_B.append({"role": "user", "content": intervention_text})
                st.session_state.discussion_log.append({
                    "role": "user",
                    "speaker": "ユーザー（あなた）",
                    "content": user_intervention,
                    "usage": None
                })

            # 2. キャラクターの発話生成
            if st.session_state.current_turn % 2 != 0:
                speaker = "アキト (数学科4年)"
                role_icon = "assistant"
                with st.spinner("🧑‍💻 アキトが思索中..."):
                    response_text, usage = get_ai_response(st.session_state.messages_A, st.session_state.current_turn, st.session_state.has_image)
                    if response_text:
                        response_text = format_math_formulas(response_text)
                        st.session_state.messages_A.append({"role": "assistant", "content": response_text})
                        st.session_state.messages_B.append({"role": "user", "content": response_text})
            else:
                speaker = "ハルタ (高校3年)"
                role_icon = "assistant"
                with st.spinner("👨‍💻 ハルタが思索中..."):
                    response_text, usage = get_ai_response(st.session_state.messages_B, st.session_state.current_turn, st.session_state.has_image)
                    if response_text:
                        response_text = format_math_formulas(response_text)
                        st.session_state.messages_B.append({"role": "assistant", "content": response_text})
                        st.session_state.messages_A.append({"role": "user", "content": response_text})
            
            # 発話が取得できた場合の保存処理
            if response_text:
                st.session_state.discussion_log.append({
                    "role": role_icon,
                    "speaker": speaker,
                    "content": response_text,
                    "code_text": response_text,
                    "usage": usage
                })
                
                if worksheet:
                    try:
                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        row_data = [
                            timestamp, MODEL, st.session_state.current_turn, 
                            speaker, response_text, 
                            usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]
                        ]
                        worksheet.append_row(row_data)
                    except Exception as sheet_err:
                        st.sidebar.error(f"⚠️ スプレッドシート保存エラー: {sheet_err}")
                
                st.session_state.current_turn += 1
                
                if "【議論終了】" in response_text:
                    st.balloons()
                    st.success("🤝 議論が円満に解決したため、ゼミを終了します。")
                
                st.rerun()
