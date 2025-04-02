import streamlit as st
import os
import re
from notion_client import Client, APIResponseError
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# --- 定数 ---
TRANSCRIPT_PARENT_PAGE_ID = "1a4a18c6848c80c1a8ecf241a3485e06"
MINUTES_PARENT_PAGE_ID = "1c9a18c6848c80afbbc3edb875805be4"
GEMINI_MODEL_NAME = "gemini-2.5-pro-exp-03-25" 

# --- APIキーの読み込み (Streamlit Secretsを使用) ---
try:
    NOTION_API_KEY = st.secrets["NOTION_API_KEY"]
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("APIキーが secrets.toml に設定されていません。")
    st.stop()
except FileNotFoundError:
    st.error(".streamlit/secrets.toml ファイルが見つかりません。")
    st.stop()


# --- Notionクライアントの初期化 ---
try:
    notion = Client(auth=NOTION_API_KEY)
except Exception as e:
    st.error(f"Notionクライアントの初期化に失敗しました: {e}")
    st.stop()

# --- Geminiクライアントの初期化 ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL_NAME)
except Exception as e:
    st.error(f"Geminiクライアントの初期化に失敗しました: {e}")
    st.stop()

# --- プロンプト ---
GEMINI_PROMPT = """
# 目的
添付された会議の文字起こしを元に議事録をつくってください。

# 注意点
・出力はmd形式で全て出力してください。
・最初に日時と参加者を明記、次に会議の目的や主題についての簡単な要約を記載し、その次に数字箇条書きで最低限のアジェンダを記載してください。
・ネクストアクションは末尾に記載し、人物と期限を必ず明確にしてください。ただし、内容は会議での事実ベースで記載し、結論付けられていないことは勝手に予測して作成しないようにしてください。（記載例：GOさん記事LPのCTA文言修正案作成【小林 〜1/15】）。
・議事録は会話内のニュアンスが失われないように丁寧に構造的に整理してください。ただし、会話調ではなく事実ベースで記載する形式にしてください。
・文量はコピペしたときにGoogleドキュメント5ページ分程度になるようにまとめ、コピペしてそのまま視覚的に見やすくなるような体裁で出力してください。

---
以下は文字起こしテキストです。
{transcript}
"""

# --- ヘルパー関数 ---

@st.cache_data(ttl=600) # 10分間キャッシュ
def get_transcript_pages(parent_page_id: str) -> list[dict]:
    """指定された親ページID直下の子ページを取得し、最新5件を返す"""
    try:
        response = notion.blocks.children.list(block_id=parent_page_id)
        child_pages = []
        for block in response.get("results", []):
            if block.get("type") == "child_page":
                child_pages.append({
                    "title": block.get("child_page", {}).get("title", "無題"),
                    "id": block.get("id")
                })

        # Notion APIは通常、追加順（≒画面上の表示順）で返すため、逆順にして最新（一番下）を先頭にする
        child_pages.reverse()
        return child_pages[:5] # 最新5件を取得
    except APIResponseError as e:
        st.error(f"Notion APIエラー (子ページ取得): {e}")
        return []
    except Exception as e:
        st.error(f"予期せぬエラー (子ページ取得): {e}")
        return []

def get_page_content(page_id: str) -> str:
    """指定されたページIDの全テキストブロックの内容を結合して返す"""
    all_text = ""
    try:
        next_cursor = None
        while True:
            response = notion.blocks.children.list(
                block_id=page_id,
                start_cursor=next_cursor
            )
            results = response.get("results", [])
            for block in results:
                block_type = block.get("type")
                if block_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "quote", "code"]: # quoteもついでに追加
                content_key = block_type # codeブロックもrich_textを持つため、特別な処理は不要
                rich_text = block.get(content_key, {}).get("rich_text", [])
                    for text_part in rich_text:
                        all_text += text_part.get("plain_text", "")
                    all_text += "\n" # 各ブロックの後に改行を追加

            next_cursor = response.get("next_cursor")
            if not next_cursor:
                break
        return all_text.strip()
    except APIResponseError as e:
        st.error(f"Notion APIエラー (ページ内容取得): {e}")
        return ""
    except Exception as e:
        st.error(f"予期せぬエラー (ページ内容取得): {e}")
        return ""

def generate_minutes_with_gemini(transcript: str) -> str:
    """Geminiを使用して議事録を生成する"""
    if not transcript:
        st.warning("文字起こしテキストが空です。")
        return ""
    try:
        prompt = GEMINI_PROMPT.format(transcript=transcript)
        # 安全性設定（必要に応じて調整）
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        response = model.generate_content(
            prompt,
            safety_settings=safety_settings
            )
        # response.text の存在を確認
        if hasattr(response, 'text'):
             # Markdownのコードブロックマーカーを除去する (例: ```markdown ... ```)
            cleaned_text = re.sub(r'^```markdown\s*|\s*```$', '', response.text, flags=re.MULTILINE).strip()
            return cleaned_text
        else:
             # 候補がない場合や他の理由でテキストがない場合のエラー処理
            st.error(f"Geminiからの応答にテキストが含まれていません。応答: {response}")
            # 応答の詳細を確認するためにプロンプトフィードバック等を確認
            if hasattr(response, 'prompt_feedback'):
                st.warning(f"Gemini Prompt Feedback: {response.prompt_feedback}")
            return ""

    except Exception as e:
        st.error(f"Gemini APIエラー: {e}")
        # エラーの詳細を表示（デバッグ用）
        st.error(f"エラータイプ: {type(e)}")
        st.error(f"エラー詳細: {e.args}")
        # 応答オブジェクトが存在すれば、その内容も表示
        if 'response' in locals() and response:
            st.json(response.__dict__) # より詳細な情報が得られる可能性
        return ""

def create_notion_page_with_markdown(parent_page_id: str, title: str, markdown_content: str) -> str | None:
    """指定された親ページIDの下に、Markdownコンテンツを持つ新しいページを作成する"""
    if not markdown_content:
        st.warning("書き込む議事録コンテンツがありません。")
        return None

    # Markdownを段落ごとに分割してNotionブロックリストを作成
    # 空行で分割し、各部分を段落ブロックとする
    blocks = []
    paragraphs = markdown_content.strip().split('\n\n') # 空行で分割

    for para in paragraphs:
        para_strip = para.strip()
        if para_strip: # 空の段落は無視
            # Notion APIは1ブロックあたりのテキスト長制限(2000文字)があるため、分割が必要な場合がある
            # ここでは簡単化のため、2000文字を超える段落はそのまま送信する（API側でエラーになる可能性あり）
            # 厳密にはループで分割する必要がある
             # 改行を保持するために '\n' を含むテキストオブジェクトにする
            text_objects = []
            lines = para_strip.split('\n')
            for i, line in enumerate(lines):
                text_objects.append({
                    "type": "text",
                    "text": {
                        "content": line
                    }
                })
                # 最後の行以外は改行を追加
                if i < len(lines) - 1:
                     text_objects.append({
                        "type": "text",
                        "text": {
                             "content": "\n"
                        }
                    })

            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": text_objects
                }
            })

            # Notion APIのブロック数制限（通常100個/リクエスト）を考慮
            if len(blocks) >= 100:
                 st.warning("コンテンツが長すぎるため、最初の約100段落のみ書き込みます。")
                 break # 多すぎる場合は途中で打ち切る


    try:
        new_page = notion.pages.create(
            parent={"page_id": parent_page_id},
            properties={
                "title": {
                    "title": [
                        {
                            "text": {
                                "content": title
                            }
                        }
                    ]
                }
            },
            children=blocks # 作成したブロックリストを渡す
        )
        st.success(f"新しい議事録ページを作成しました！")
        # NotionページのURLを返す (形式: https://www.notion.so/PAGE_ID_WITHOUT_HYPHENS)
        page_id_no_hyphen = new_page.get('id', '').replace('-', '')
        page_url = f"https://www.notion.so/{page_id_no_hyphen}"
        st.markdown(f"**[新しいページを開く]({page_url})**")
        return new_page.get("id")
    except APIResponseError as e:
        st.error(f"Notion APIエラー (ページ作成): {e}")
        # エラーレスポンスの内容を表示してみる（デバッグ用）
        st.json(e.body)
        return None
    except Exception as e:
        st.error(f"予期せぬエラー (ページ作成): {e}")
        return None


# --- Streamlit アプリのUI ---

st.set_page_config(page_title="Notion議事録ジェネレーター", layout="wide")
st.title("📄 Notion 文字起こしから議事録を生成")
st.caption("文字起こしが格納されたNotionページを選択し、Geminiで議事録を作成してNotionに保存します。")

# --- ① 文字起こしページの選択 ---
st.header("1. 文字起こしページの選択")
transcript_pages = get_transcript_pages(TRANSCRIPT_PARENT_PAGE_ID)

if not transcript_pages:
    st.warning("文字起こし格納ページに子ページが見つからないか、取得に失敗しました。Notionの連携設定やページ構造を確認してください。")
else:
    page_options = {page["title"]: page["id"] for page in transcript_pages}
    selected_page_title = st.selectbox(
        "議事録を作成したい文字起こしページを選択してください（最新5件）:",
        options=page_options.keys()
    )
    selected_page_id = page_options[selected_page_title]

    st.write(f"選択中のページ: **{selected_page_title}** (ID: `{selected_page_id}`)")

    # --- ② 議事録生成ボタン ---
    st.header("2. 議事録の生成")
    if st.button("📝 議事録を生成する"):
        if selected_page_id:
            with st.spinner(f"'{selected_page_title}' の内容を取得中..."):
                transcript_text = get_page_content(selected_page_id)

            if transcript_text:
                st.info(f"文字起こし内容（{len(transcript_text)}文字）を取得しました。")
                with st.spinner(f"Gemini ({GEMINI_MODEL_NAME}) で議事録を生成中..."):
                    generated_minutes_md = generate_minutes_with_gemini(transcript_text)

                if generated_minutes_md:
                    st.subheader("✨ 生成された議事録 (Markdown)")
                    st.markdown(generated_minutes_md) # Markdown形式でプレビュー

                    # --- ③ Notionへの保存 ---
                    st.header("3. Notionに保存")
                    new_page_title = f"{selected_page_title} - 議事録" # 新しいページタイトル
                    with st.spinner(f"Notionの「議事録格納ページ」に '{new_page_title}' を作成中..."):
                         create_notion_page_with_markdown(
                            parent_page_id=MINUTES_PARENT_PAGE_ID,
                            title=new_page_title,
                            markdown_content=generated_minutes_md
                        )

                else:
                    st.error("議事録の生成に失敗しました。")
            else:
                st.error(f"'{selected_page_title}' から文字起こし内容を取得できませんでした。ページが空か、アクセス権限がない可能性があります。")
        else:
            st.warning("文字起こしページが選択されていません。")

st.divider()
st.caption("Powered by Streamlit, Notion API, and Google Gemini")
