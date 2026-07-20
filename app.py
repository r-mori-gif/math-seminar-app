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

# セッション状態（State）の初期化
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

# API設定
openrouter_api_key = st.secrets.get("OPENROUTER_API_KEY")
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=openrouter_api_key,
)
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
SPREADSHEET_ID = "1_sRw5amkLY_-O0sj2VlIJklr05DSXJ3Lf8DIYxVSw5c"

# Googleスプレッドシートの認証
@st.cache_resource
def init_gspread():
    try:
        creds_dict = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        return spreadsheet.get_worksheet(0)
    except Exception as e:
        st.error(f"❌ スプレッドシートの接続に失敗しました: {e}")
        return None

worksheet = init_gspread()

# ----------------------------------------------------
# 2. 定数（プロンプト等）
# ----------------------------------------------------
MATH_FORMAT_INSTRUCTION = """
【数式記述の厳格ルール】
Markdownが数式を正しくレンダリングできるように、以下のルールを必ず守ってください。
1. 独立した行で数式を表示する場合（別行立て数式）は、必ず `$$` で囲んでください。
2. 文章中に数式を挿入する場合（インライン数式）は、必ず単一の `$` で囲んでください。
3. LaTeXのコマンド（`\\sin`, `\\cos` など）のバックスラッシュが消えないよう、正確に記述してください。
"""

INSTRUCTION_A_TEMPLATE = """あなたはある数学ゼミに所属する理系大学生の「アキト」です。
友人の「ハルタ（高校三年生）」と一緒に、提示された数学の問題について話し合って解法を導き出してください。
（中略、元の指示をここに記載してください）
""" + MATH_FORMAT_INSTRUCTION

INSTRUCTION_B_TEMPLATE = """あなたは高校三年生の「ハルタ」です。
理系大学生の先輩である「アキト」と一緒に、提示された数学の問題について話し合って解法を導き出してください。
（中略、元の指示をここに記載してください）
""" + MATH_FORMAT_INSTRUCTION

# ----------------------------------------------------
# 3. ユーティリティ関数
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
        st.caption("🎨 **エージェントが図形を描画しました:**")
        try:
            local_vars = {}
            fig, ax = plt.subplots()
            exec(code, globals(), local_vars)
            st.pyplot(fig)
            plt.close(fig)
        except Exception as e:
            st.warning(f"⚠️ 描画コードの実行中にエラーが発生しました: {e}")

def get_ai_response(messages, turn, has_image):
    keep_image = (not has_image) or (turn <= 2)
    api_messages = clean_messages_for_api(messages, keep_image=keep_image)
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            max_tokens=2048
        )
        res_text = response.choices[0].message.content.strip()
        usage = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(response.usage, "completion_tokens", 0),
            "total_tokens": getattr(response.usage, "total_tokens", 0)
        }
        return res_text, usage
    except Exception as e:
        st.error(f"APIエラーが発生しました: {e}")
        return None, None

# ----------------------------------------------------
# 4. UI 描画
# ----------------------------------------------------
if not st.session_state.system_started:
    # ── 入力設定画面 ──
    st.subheader("📐 ゼミ議論の条件設定")
    choice = st.radio("数学問題の入力形式を選択してください", ("テキスト直接入力", "PNG画像をアップロード"))
    
    problem_input = ""
    image_content = None
    
    if choice == "PNG画像をアップロード":
        uploaded_file = st.file_uploader("問題を写したPNG画像をアップロードしてください", type=["png"])
        if uploaded_file:
            bytes_data = uploaded_file.read()
            image_base64 = base64.b64encode(bytes_data).decode('utf-8')
            image_content = {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"}
            }
            problem_input = "[アップロード画像の問題]"
    else:
        problem_input = st.text_area("考えて欲しい数学の問題を入力してください:", height=150)

    if st.button("ゼミ議論を開始する", disabled=not (problem_input or image_content)):
        # 初回起動時のデータリセットとプロンプトの構成
        st.session_state.has_image = (image_content is not None)
        st.session_state.problem_text = problem_input
        st.session_state.image_content = image_content
        
        problem_desc = "提示された画像の問題" if st.session_state.has_image else problem_input
        
        inst_A = INSTRUCTION_A_TEMPLATE.replace("{problem_description}", problem_desc)
        inst_B = INSTRUCTION_B_TEMPLATE.replace("{problem_description}", problem_desc)
        
        # 履歴構築
        if st.session_state.has_image:
            st.session_state.messages_A = [
                {"role": "system", "content": inst_A},
                {"role": "user", "content": [
                    {"type": "text", "text": "こちらの数学の問題を一緒に解き明かしてください。アキト、まずは君から最初の気づきを話して議論をスタートして。"},
                    image_content
                ]}
            ]
            st.session_state.messages_B = [
                {"role": "system", "content": inst_B},
                {"role": "user", "content": [
                    {"type": "text", "text": "こちらの数学の問題を一緒に解き明かしてください。"},
                    image_content
                ]}
            ]
        else:
            st.session_state.messages_A = [
                {"role": "system", "content": inst_A},
                {"role": "user", "content": "こちらの数学の問題を一緒に解き明かしてください。アキト、まずは君から最初の気づきを話して議論をスタートして。"}
            ]
            st.session_state.messages_B = [
                {"role": "system", "content": inst_B},
                {"role": "user", "content": "こちらの数学の問題を一緒に解き明かしてください。"}
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
            st.text_area("問題文:", value=st.session_state.problem_text, disabled=True, height=100)
            
        if st.button("セッションを初期化（やり直し）"):
            for key in ["messages_A", "messages_B", "current_turn", "discussion_log", "system_started", "problem_text", "has_image", "image_content"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    with col_main:
        st.subheader("💬 議論ログ")
        
        # 保存されている発言ログを吹き出し風にレンダリング
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
        
        # 進行およびユーザー介入フォーム
        st.divider()
        with st.form("control_form", clear_on_submit=True):
            user_intervention = st.text_input("💬 介入：アキトやハルタに質問・指示を与える（空欄なら通常どおり自動進行）")
            submitted = st.form_submit_button("➡️ 次のターンへ進む")
            
        if submitted:
            # 1. ユーザーからの介入があれば履歴に追加
            if user_intervention:
                intervention_text = f"【ユーザーからの質問・意見】:\n{user_input}\n\nこの意見も踏まえて、議論を続けてください。"
                st.session_state.messages_A.append({"role": "user", "content": intervention_text})
                st.session_state.messages_B.append({"role": "user", "content": intervention_text})
                
                # ログに表示
                st.session_state.discussion_log.append({
                    "role": "user",
                    "speaker": "ユーザー（あなた）",
                    "content": user_intervention
                })
                st.rerun()

            # 2. どちらが話すか決定
            if st.session_state.current_turn % 2 != 0:
                speaker = "アキト (数学科4年)"
                role_icon = "assistant"
                # アキトのターン
                with st.spinner("🧑‍💻 アキトが考えています..."):
                    response_text, usage = get_ai_response(st.session_state.messages_A, st.session_state.current_turn, st.session_state.has_image)
                    if response_text:
                        response_text = format_math_formulas(response_text)
                        st.session_state.messages_A.append({"role": "assistant", "content": response_text})
                        st.session_state.messages_B.append({"role": "user", "content": response_text})
            else:
                speaker = "ハルタ (高校3年)"
                role_icon = "assistant"
                # ハルタのターン
                with st.spinner("👨‍💻 ハルタが考えています..."):
                    response_text, usage = get_ai_response(st.session_state.messages_B, st.session_state.current_turn, st.session_state.has_image)
                    if response_text:
                        response_text = format_math_formulas(response_text)
                        st.session_state.messages_B.append({"role": "assistant", "content": response_text})
                        st.session_state.messages_A.append({"role": "user", "content": response_text})
            
            if response_text:
                # ログに保存
                log_entry = {
                    "role": role_icon,
                    "speaker": speaker,
                    "content": response_text,
                    "code_text": response_text,  # execによる図形描画用
                    "usage": usage
                }
                st.session_state.discussion_log.append(log_entry)
                
                # スプレッドシート保存
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
                        st.sidebar.error(f"スプレッドシート保存エラー: {sheet_err}")
                
                # ターンを進める
                st.session_state.current_turn += 1
                
                if "【議論終了】" in response_text:
                    st.success("🤝 議論が円満に解決したため、ゼミを終了します。")
                
                st.rerun()
