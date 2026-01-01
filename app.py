import streamlit as st
import requests
import pandas as pd
import pytz
import datetime
import io
from streamlit_autorefresh import st_autorefresh
import ftplib
import io
import time
import datetime
import os
from free_gift_handler import FreeGiftReceiver, get_streaming_server_info, update_free_gift_master, gift_queue


def upload_csv_to_ftp(filename: str, csv_buffer: io.BytesIO):
    """Secretsに登録されたFTP設定を使ってCSVをアップロード"""
    ftp_info = st.secrets["ftp"]
    try:
        ftp = ftplib.FTP(ftp_info["host"])
        ftp.login(ftp_info["user"], ftp_info["password"])
        ftp.cwd("/rokudouji.net/mksoul/showroom_onlives_logs")

        # アップロード
        csv_buffer.seek(0)
        ftp.storbinary(f"STOR {filename}", csv_buffer)

        # --- 古いファイル削除（48時間以上前） ---
        file_list = []
        ftp.retrlines("LIST", file_list.append)
        now = datetime.datetime.now()
        for entry in file_list:
            parts = entry.split(maxsplit=8)
            if len(parts) < 9:
                continue
            name = parts[-1]
            if not name.endswith(".csv"):
                continue
            # 日時文字列が含まれる形式なら抽出
            try:
                time_str = name.split("_")[-1].replace(".csv", "")
                file_dt = datetime.datetime.strptime(time_str, "%Y%m%d_%H%M%S")
                if (now - file_dt).total_seconds() > 48 * 3600:
                    ftp.delete(name)
            except Exception:
                continue

        ftp.quit()
        st.success(f"✅ FTPに保存完了: {filename}")
    except Exception as e:
        st.error(f"FTP保存中にエラー: {e}")


def auto_backup_if_needed():
    """100件ごとまたはトラッキング停止時にFTPへログをバックアップ"""
    room = st.session_state.room_id
    # 必要ログが無ければスキップ
    if not room:
        return

    # 条件：コメント＋ギフトの合計が100件ごと または トラッキング停止時
    total = len(st.session_state.comment_log) + len(st.session_state.gift_log)
    if total == 0:
        return

    # トラッキング停止時強制保存 or 100件ごと保存
    if (not st.session_state.is_tracking) or (total % 100 == 0):
        timestamp = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        filename = f"srlog_{room}_{timestamp}.csv"
        buf = io.StringIO()
        # コメントログ
        if st.session_state.comment_log:
            df_c = pd.DataFrame(st.session_state.comment_log)
            buf.write("### Comments\n")
            df_c.to_csv(buf, index=False, encoding='utf-8-sig')
        # ギフトログ
        if st.session_state.gift_log:
            buf.write("\n### Gifts\n")
            df_g = pd.DataFrame(st.session_state.gift_log)
            df_g.to_csv(buf, index=False, encoding='utf-8-sig')

        content = buf.getvalue().encode("utf-8-sig")
        upload_to_ftp(content, filename)


# --- ▼ 共通FTP保存関数（コメント・ギフトログ用） ▼ ---
def save_log_to_ftp(log_type: str):
    """
    コメント or ギフトログをFTPに保存
    log_type: "comment" または "gift"
    """
    try:
        room = st.session_state.room_id
        if not room:
            return

        timestamp = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")

        # ===== コメントログ処理 =====
        if log_type == "comment":
            filtered_comments = [
                log for log in st.session_state.comment_log
                if not any(keyword in log.get('name', '') or keyword in log.get('comment', '')
                           for keyword in SYSTEM_COMMENT_KEYWORDS)
            ]
            if not filtered_comments:
                return

            comment_df = pd.DataFrame(filtered_comments)
            comment_df['created_at'] = pd.to_datetime(comment_df['created_at'], unit='s') \
                .dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
            comment_df['user_id'] = [log.get('user_id', 'N/A') for log in filtered_comments]
            comment_df = comment_df.rename(columns={
                'name': 'ユーザー名',
                'comment': 'コメント内容',
                'created_at': 'コメント時間',
                'user_id': 'ユーザーID'
            })
            cols = ['コメント時間', 'ユーザー名', 'コメント内容', 'ユーザーID']
            buf = io.BytesIO()
            comment_df[cols].to_csv(buf, index=False, encoding='utf-8-sig')
            buf.seek(0)
            filename = f"comment_log_{room}_{timestamp}.csv"
            upload_csv_to_ftp(filename, buf)

        # ===== ギフトログ処理 =====
        elif log_type == "gift":
            if not st.session_state.gift_log:
                return
            gift_df = pd.DataFrame(st.session_state.gift_log)
            gift_df['created_at'] = pd.to_datetime(gift_df['created_at'], unit='s') \
                .dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")

            if st.session_state.gift_list_map:
                gift_info_df = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
                gift_info_df.index = gift_info_df.index.astype(str)
                gift_df['gift_id'] = gift_df['gift_id'].astype(str)
                gift_df = gift_df.set_index('gift_id') \
                    .join(gift_info_df, on='gift_id', lsuffix='_user_data', rsuffix='_gift_info') \
                    .reset_index()

            gift_df = gift_df.rename(columns={
                'name_user_data': 'ユーザー名',
                'name_gift_info': 'ギフト名',
                'num': '個数',
                'point': 'ポイント',
                'created_at': 'ギフト時間',
                'user_id': 'ユーザーID'
            })
            cols = ['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']
            buf = io.BytesIO()
            gift_df[cols].to_csv(buf, index=False, encoding='utf-8-sig')
            buf.seek(0)
            filename = f"gift_log_{room}_{timestamp}.csv"
            upload_csv_to_ftp(filename, buf)
    except Exception as e:
        st.error(f"ログ保存中にエラー: {e}")



# ページ設定
st.set_page_config(
    page_title="SHOWROOM 配信ログ収集ツール",
    page_icon="🎤",
    layout="wide",
)

# 定数
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
}
JST = pytz.timezone('Asia/Tokyo')
ONLIVES_API_URL = "https://www.showroom-live.com/api/live/onlives"
COMMENT_API_URL = "https://www.showroom-live.com/api/live/comment_log"
GIFT_API_URL = "https://www.showroom-live.com/api/live/gift_log"
GIFT_LIST_API_URL = "https://www.showroom-live.com/api/live/gift_list"
FAN_LIST_API_URL = "https://www.showroom-live.com/api/active_fan/users"
SYSTEM_COMMENT_KEYWORDS = ["SHOWROOM Management", "Earn weekly glittery rewards!", "ウィークリーグリッター特典獲得中！", "SHOWROOM運営"]
DEFAULT_AVATAR = "https://static.showroom-live.com/image/avatar/default_avatar.png"
ROOM_LIST_URL = "https://mksoul-pro.com/showroom/file/room_list.csv"

if "authenticated" not in st.session_state:  #認証用
    st.session_state.authenticated = False  #認証用

# CSSスタイル
CSS_STYLE = """
<style>
.dashboard-container {
    height: 500px;
    overflow-y: scroll;
    padding-right: 15px;
}
.comment-item-row, .gift-item-row, .fan-info-row {
    display: flex;
    align-items: center;
    gap: 10px;
}
.comment-avatar, .gift-avatar, .fan-avatar {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    object-fit: cover;
}
.comment-content, .gift-content, .fan-content {
    flex-grow: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
}
.comment-time, .gift-time {
    font-size: 0.8em;
    color: #888;
}
.comment-user, .gift-user, .fan-user {
    font-weight: bold;
    color: #333;
    word-wrap: break-word;
}
.comment-text {
    margin-top: 4px;
}
.gift-info-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
    margin-bottom: 4px;
}
.gift-image {
    width: 30px;
    height: 30px;
    object-fit: contain;
}
.highlight-10000 { background-color: #ffe5e5; }
.highlight-30000 { background-color: #ffcccc; }
.highlight-60000 { background-color: #ffb2b2; }
.highlight-100000 { background-color: #ff9999; }
.highlight-300000 { background-color: #ff7f7f; }
.fan-level {
    font-weight: bold;
    color: #555;
}
.tracking-success {
    background-color: #e6f7e6;
    color: #333333;
    padding: 1rem;
    border-left: 5px solid #4CAF50;
    /*margin-bottom: 5px !important;*/
    margin-bottom: -36px !important;
    margin-top: 0 !important;
    position: relative; 
    z-index: 9999;      /* 強制的に一番手前に表示させる */
}
/* 配信終了用（追加：色とボーダー以外は全て同じ） */
.tracking-info {
    background-color: #f0f2f6; /* 終了とわかるグレー系 */
    color: #333333;
    padding: 1rem;
    border-left: 5px solid #ff4b4b; /* 終了を示す赤色の線 */
    margin-bottom: -36px !important;
    margin-top: 0 !important;
    position: relative; 
    z-index: 9999;
}
</style>
"""
st.markdown(CSS_STYLE, unsafe_allow_html=True)

# エラーメッセージ・警告メッセージの幅を100%に変更
CUSTOM_MSG_CSS = """
<style>
/* 通常の警告・情報用 */
div[data-testid="stNotification"] {
    width: 100% !important;
    max-width: 100% !important;
}

/* st.error 専用: Streamlit 1.38+ では .stAlert クラスを使用 */
div.stAlert {
    width: 100% !important;
    max-width: 100% !important;
}

/* 追加の親要素にも適用（念のため） */
section.main div.block-container {
    width: 100% !important;
}
</style>
"""
st.markdown(CUSTOM_MSG_CSS, unsafe_allow_html=True)


# セッション状態の初期化
if "room_id" not in st.session_state:
    st.session_state.room_id = ""
if "is_tracking" not in st.session_state:
    st.session_state.is_tracking = False
if "comment_log" not in st.session_state:
    st.session_state.comment_log = []
if "gift_log" not in st.session_state:
    st.session_state.gift_log = []
if "fan_list" not in st.session_state:
    st.session_state.fan_list = []
if "gift_list_map" not in st.session_state:
    st.session_state.gift_list_map = {}
if 'onlives_data' not in st.session_state:
    st.session_state.onlives_data = {}
if 'total_fan_count' not in st.session_state:
    st.session_state.total_fan_count = 0

# --- 無償ギフト用に追加 ---
if "free_gift_log" not in st.session_state:
    st.session_state.free_gift_log = []
if "raw_free_gift_queue" not in st.session_state:
    st.session_state.raw_free_gift_queue = []
if "free_gift_master" not in st.session_state:
    st.session_state.free_gift_master = {} # {gift_id: {name, point, image}}
if "ws_receiver" not in st.session_state:
    st.session_state.ws_receiver = None
# -----------------------

# --- API連携関数 ---

def get_onlives_rooms():
    onlives = {}
    try:
        response = requests.get(ONLIVES_API_URL, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        all_lives = []
        if isinstance(data, dict):
            if 'onlives' in data and isinstance(data['onlives'], list):
                for genre_group in data['onlives']:
                    if 'lives' in genre_group and isinstance(genre_group['lives'], list):
                        all_lives.extend(genre_group['lives'])
            for live_type in ['official_lives', 'talent_lives', 'amateur_lives']:
                if live_type in data and isinstance(data.get(live_type), list):
                    all_lives.extend(data[live_type])
        for room in all_lives:
            room_id = None
            if isinstance(room, dict):
                room_id = room.get('room_id')
                if room_id is None and 'live_info' in room and isinstance(room['live_info'], dict):
                    room_id = room['live_info'].get('room_id')
                if room_id is None and 'room' in room and isinstance(room['room'], dict):
                    room_id = room['room'].get('room_id')
            if room_id:
                onlives[int(room_id)] = room
    except requests.exceptions.RequestException as e:
        st.error(f"配信情報取得中にエラーが発生しました: {e}")
    except (ValueError, AttributeError):
        st.error("配信情報のJSONデコードまたは解析に失敗しました。")
    return onlives

def get_and_update_log(log_type, room_id):
    api_url = COMMENT_API_URL if log_type == "comment" else GIFT_API_URL
    url = f"{api_url}?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        new_log = response.json().get(f'{log_type}_log', [])
        existing_cache = st.session_state[f"{log_type}_log"]
        existing_log_keys = {(log.get('created_at'), log.get('name')) for log in existing_cache}
        for log in new_log:
            log_key = (log.get('created_at'), log.get('name'))
            if log_key not in existing_log_keys:
                existing_cache.append(log)
                existing_log_keys.add(log_key)
        existing_cache.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        return existing_cache
    except requests.exceptions.RequestException:
        st.warning(f"ルームID {room_id} の{log_type}ログ取得中にエラーが発生しました。配信中か確認してください。")
        return st.session_state.get(f"{log_type}_log", [])

def get_gift_list(room_id, force_update=False):
    """
    ギフトリストを取得しキャッシュする。
    force_update=True、または未知のギフトIDに遭遇した際に再取得を行う。
    """
    # キャッシュがあり、強制更新でない場合はそのまま返す
    if st.session_state.gift_list_map and not force_update:
        return st.session_state.gift_list_map

    url = f"{GIFT_LIST_API_URL}?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        new_map = {}
        # すべてのカテゴリ（normal, special, enquete, seasonal等）を網羅的に走査
        for category_key in data.keys():
            category_items = data.get(category_key, [])
            if isinstance(category_items, list):
                for gift in category_items:
                    gid = str(gift.get('gift_id'))
                    try:
                        p = int(gift.get('point', 0))
                    except (ValueError, TypeError):
                        p = 0
                    
                    new_map[gid] = {
                        'name': gift.get('gift_name', 'N/A'),
                        'point': p,
                        'image': gift.get('image', ''),
                        'free': gift.get('free', False)
                    }
        
        st.session_state.gift_list_map = new_map
        return new_map
    except Exception as e:
        print(f"Gift List API Error: {e}")
        return st.session_state.get('gift_list_map', {})


def get_fan_list(room_id):
    fan_list = []
    offset = 0
    limit = 50
    current_ym = datetime.datetime.now(JST).strftime("%Y%m")
    total_user_count = 0
    while True:
        url = f"{FAN_LIST_API_URL}?room_id={room_id}&ym={current_ym}&offset={offset}&limit={limit}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=5)
            response.raise_for_status()
            data = response.json()
            users = data.get("users", [])
            if offset == 0 and "total_user_count" in data:
                total_user_count = data["total_user_count"]
            if not users:
                break
            for user in users:
                if user.get('level', 0) < 10:
                    return fan_list, total_user_count
                fan_list.append(user)
            offset += len(users)
            if len(users) < limit:
                break
        except requests.exceptions.RequestException:
            st.warning(f"ルームID {room_id} のファンリスト取得中にエラーが発生しました。")
            break
    return fan_list, total_user_count

# --- ルームリスト取得関数 ---
def get_room_list():
    try:
        df = pd.read_csv(ROOM_LIST_URL)
        return df
    except Exception:
        return pd.DataFrame()


def update_free_gift_master(room_id):
    """ギフトリストAPIから無償ギフト(free=True)のみを抽出し、セッション状態のマスターを更新する"""
    url = f"https://www.showroom-live.com/api/live/gift_list?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # normal などのリストの中にギフト情報が入っている
        new_master = {}
        # normal, special, enquete など複数のカテゴリを走査
        for category in data.values():
            if isinstance(category, list):
                for gift in category:
                    # フリー かつ point が 1 のものだけをマスターに登録する
                    if gift.get("free") == True and gift.get("point") == 1:
                        new_master[gift.get("gift_id")] = {
                            "name": gift.get("gift_name"),
                            "point": gift.get("point", 0),
                            "image": gift.get("image")
                        }
        st.session_state.free_gift_master = new_master
    except Exception as e:
        st.error(f"ギフトリストの取得に失敗しました: {e}")


# --- UI構築 ---

#st.markdown("<h1 style='font-size:2.5em;'>🎤 SHOWROOM 配信ログ収集ツール</h1>", unsafe_allow_html=True)
st.markdown(
    "<h1 style='font-size:28px; text-align:left; color:#1f2937;'>🎤 SHOWROOM 配信ログ収集ツール</h1>",
    unsafe_allow_html=True
)
st.write("配信中のコメント、スペシャルギフト、無償ギフト、ファンリストをリアルタイムで収集し、ログをダウンロードできます。")
st.write("")


# ▼▼ 認証ステップ ▼▼
if not st.session_state.authenticated:
    st.markdown("##### 🔑 認証コードを入力してください")
    input_room_id = st.text_input(
        "認証コードを入力してください:",
        placeholder="",
        type="password",
        key="room_id_input"
    )

    # 認証ボタン
    if st.button("認証する"):
        if input_room_id:  # 入力が空でない場合のみ
            try:
                response = requests.get(ROOM_LIST_URL, timeout=5)
                response.raise_for_status()
                room_df = pd.read_csv(io.StringIO(response.text), header=None)

                valid_codes = set(str(x).strip() for x in room_df.iloc[:, 0].dropna())

                # ✅ 特別認証コード「mksp154851」なら全ルーム利用可
                if input_room_id.strip() == "mksp154851":
                    st.session_state.authenticated = True
                    st.session_state.is_master_access = True  # フラグを立てる
                    st.success("✅ 特別認証モード（全ルーム対応）でログ取得が可能です。")
                    st.rerun()

                elif input_room_id.strip() in valid_codes:
                    st.session_state.authenticated = True
                    st.session_state.is_master_access = False
                    st.success("✅ 認証に成功しました。ツールを利用できます。")
                    st.rerun()

                else:
                    st.error("❌ 認証コードが無効です。正しい認証コードを入力してください。")
            except Exception as e:
                st.error(f"認証リストを取得できませんでした: {e}")
        else:
            st.warning("認証コードを入力してください。")

    # 認証が終わるまで他のUIを描画しない
    st.stop()
# ▲▲ 認証ステップここまで ▲▲


input_room_id = st.text_input("対象のルームIDを入力してください:", placeholder="例: 154851", key="target_room_id_input")

# --- ボタンを縦並びに配置 ---
if st.button("トラッキング開始", key="start_button"):
    if input_room_id and input_room_id.isdigit():
        room_list_df = get_room_list()
        valid_ids = set(str(x) for x in room_list_df.iloc[:,0].dropna().astype(int))

        # ✅ 特別認証モード（mksp154851）の場合はバイパス許可
        is_master = st.session_state.get("is_master_access", False)
        if not is_master and input_room_id not in valid_ids:
            # エラー時は状態を更新せず、メッセージだけ出す（下の停止ボタンは非活性のまま残る）
            st.error("指定されたルームIDが見つからないか、認証されていないルームIDか、現在配信中ではありません。")
        else:
            # 配信サーバー情報を取得
            streaming_info = get_streaming_server_info(input_room_id)
            
            if not streaming_info:
                # サーバー情報が取れない（配信中でない）場合もエラー表示のみ
                st.error("指定されたルームIDが見つからないか、認証されていないルームIDか、現在配信中ではありません。")
            else:
                # --- 正常系：ここから下は配信中であることが確定した場合のみ実行 ---
                st.session_state.is_tracking = True
                st.session_state.room_id = input_room_id
                
                # --- 既存ログの初期化 ---
                st.session_state.comment_log = []
                st.session_state.gift_log = []
                st.session_state.gift_list_map = {}
                st.session_state.fan_list = []
                st.session_state.total_fan_count = 0
                st.session_state.free_gift_log = []
                st.session_state.raw_free_gift_queue = []
                st.session_state.system_msg_log = []
                
                # 1. 無償ギフトマスターの取得
                update_free_gift_master(input_room_id)
                
                # 2. 受信機の起動
                if st.session_state.get("ws_receiver"):
                    try:
                        st.session_state.ws_receiver.stop()
                    except:
                        pass
                
                receiver = FreeGiftReceiver(
                    room_id=input_room_id,
                    host=streaming_info["host"],
                    key=streaming_info["key"]
                )
                receiver.start()
                st.session_state.ws_receiver = receiver

                # 成功時のみ画面を更新して「ログ詳細」を表示
                st.rerun()
    else:
        st.error("ルームIDを入力してください。")

if st.button("トラッキング停止", key="stop_button", disabled=not st.session_state.is_tracking):
    if st.session_state.is_tracking:
        save_log_to_ftp("comment")
        save_log_to_ftp("gift")
        save_log_to_ftp("free_gift")
        save_log_to_ftp("system_msg")

    st.session_state.is_tracking = False
    st.session_state.room_info = None
    st.success("トラッキングを停止しました。このままログの確認・ダウンロードが可能です。")
    # st.rerun()  # ← ここをコメントアウトして即時リセットを防ぐ


if st.session_state.is_tracking or st.session_state.get("room_id"):
    onlives_data = get_onlives_rooms()
    target_room_info = onlives_data.get(int(st.session_state.room_id)) if st.session_state.room_id.isdigit() else None

    # --- 配信終了検知と自動保存処理 ---
    # インデントを一段（半角スペース4つ）に統一しています
    is_live_now = int(st.session_state.room_id) in onlives_data

    if not is_live_now:
        # st.warning("📡 配信が終了しました。全ログを最終保存します。")
        st.info("📡 配信の終了を確認しました。未保存のログを含め、最終データを保存します。")

        # 1. コメントログ保存
        if st.session_state.comment_log:
            comment_df = pd.DataFrame([
                {
                    "コメント時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                    "ユーザー名": log.get("name", ""),
                    "コメント内容": log.get("comment", ""),
                    "ユーザーID": log.get("user_id", "")
                }
                for log in st.session_state.comment_log
                if not any(keyword in log.get("name", "") or keyword in log.get("comment", "") for keyword in SYSTEM_COMMENT_KEYWORDS)
            ])
            buf = io.BytesIO()
            comment_df.to_csv(buf, index=False, encoding="utf-8-sig")
            upload_csv_to_ftp(f"comment_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)

        # 2. 有償ギフトログ保存
        if st.session_state.gift_log:
            gift_df = pd.DataFrame([
                {
                    "ギフト時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                    "ユーザー名": log.get("name", ""),
                    "ギフト名": st.session_state.gift_list_map.get(str(log.get("gift_id")), {}).get("name", ""),
                    "個数": log.get("num", ""),
                    "ポイント": st.session_state.gift_list_map.get(str(log.get("gift_id")), {}).get("point", 0),
                    "ユーザーID": log.get("user_id", "")
                }
                for log in st.session_state.gift_log
            ])
            buf = io.BytesIO()
            gift_df.to_csv(buf, index=False, encoding="utf-8-sig")
            upload_csv_to_ftp(f"gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)

        # 3. 無償ギフトログ保存（追加分）
        if st.session_state.free_gift_log:
            free_gift_df = pd.DataFrame([
                {
                    "ギフト時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                    "ユーザー名": log.get("name", ""),
                    "ギフト名": log.get("gift_name", ""),
                    "個数": log.get("num", ""),
                    "ポイント": log.get("point", 0),
                    "ユーザーID": log.get("user_id", "")
                }
                for log in st.session_state.free_gift_log
            ])
            buf = io.BytesIO()
            free_gift_df.to_csv(buf, index=False, encoding="utf-8-sig")
            upload_csv_to_ftp(f"free_gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)

        # 4. システムメッセージログ保存（追加）
        if st.session_state.system_msg_log:
            system_msg_df = pd.DataFrame([
                {
                    "時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                    "メッセージ": log.get("message", ""),
                    "ユーザーID": log.get("user_id", "")
                }
                for log in st.session_state.system_msg_log
            ])
            buf = io.BytesIO()
            system_msg_df.to_csv(buf, index=False, encoding="utf-8-sig")
            upload_csv_to_ftp(f"system_msg_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)

        # 配信が終了しても、表示用のフラグを「停止」にせず、警告を出すだけにする
        # st.session_state.is_tracking = False  # 消去またはコメントアウト
        # st.warning("📡 配信が終了しました。ログを最終保存しました。このまま振り返りが可能です。")
        # st.success("✅ 最終保存が完了しました。このままデータの確認や手動ダウンロードが可能です。")
        st.success("✅ 最終保存が完了しました。自動更新を停止し、現在のログを保持しています。このままデータの確認やダウンロードが可能です。")
        # st.rerun()  # 消去またはコメントアウト


    if target_room_info or st.session_state.get("room_id"):
        room_id = st.session_state.room_id

        # ルーム名取得
        try:
            prof = requests.get(f"https://www.showroom-live.com/api/room/profile?room_id={room_id}", headers=HEADERS, timeout=5).json()
            room_name = prof.get("room_name", f"ルームID {room_id}")
        except Exception:
            room_name = f"ルームID {room_id}"
        # URLキー取得
        room_url_key = prof.get("room_url_key", "")
        room_url = f"https://www.showroom-live.com/r/{room_url_key}" if room_url_key else f"https://www.showroom-live.com/room/profile?room_id={room_id}"
        link_html = f'<a href="{room_url}" target="_blank" style="font-weight:bold; text-decoration:underline; color:inherit;">{room_name}</a>'        
        # 配信状態によってメッセージを切り替える
        if is_live_now:
            # 配信中（緑色）
            st.markdown(f'<div class="tracking-success">📡 {link_html} の配信をトラッキング中です。</div>', unsafe_allow_html=True)
        else:
            # 💡 ここを直接書き込みから「クラス指定」に変更します
            st.markdown(f'<div class="tracking-info">🏁 {link_html} の配信は終了しました。</div>', unsafe_allow_html=True)

        # 配信中の時だけ自動更新し、新しいログを取得しにいく
        if is_live_now:
            st_autorefresh(interval=10000, limit=None, key="dashboard_refresh")
            st.session_state.comment_log = get_and_update_log("comment", st.session_state.room_id)
            st.session_state.gift_log = get_and_update_log("gift", st.session_state.room_id)
        else:
            # 💡 ここにあった st.info を削除（またはコメントアウト）します
            # st.info("配信が終了したため、自動更新を停止しました。現在のログを保持しています。")
            pass

        import math

        # コメントログ自動保存
        prev_comment_count = st.session_state.get("prev_comment_count", 0)
        current_comment_count = len(st.session_state.comment_log)

        # 💡 修正後の保存しきい値: prev_comment_countを次の100の倍数に丸めた値
        # 例: prev_countが105の場合、次の保存しきい値は200
        # 例: prev_countが100の場合、次の保存しきい値は200
        next_save_threshold = math.ceil((prev_comment_count + 1) / 100) * 100

        # 🌟 条件判定: 現在の総数が次の100の倍数のしきい値以上になったら保存
        if current_comment_count >= next_save_threshold:
            if current_comment_count > 0:
                comment_df = pd.DataFrame([
                    # ... DataFrame生成の処理は省略 ...
                    # 既存のコードのまま、全ログをDataFrameに変換
                    {
                        "コメント時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                        "ユーザー名": log.get("name", ""),
                        "コメント内容": log.get("comment", ""),
                        "ユーザーID": log.get("user_id", "")
                    }
                    for log in st.session_state.comment_log
                    if not any(keyword in log.get("name", "") or keyword in log.get("comment", "") for keyword in SYSTEM_COMMENT_KEYWORDS)
                ])
                
                buf = io.BytesIO()
                comment_df.to_csv(buf, index=False, encoding="utf-8-sig")
                upload_csv_to_ftp(f"comment_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)
                
                # 🌟 変更点: 次に保存すべき件数 (100の倍数) に更新する
                # ここで `current_comment_count` ではなく `next_save_threshold` を使用
                st.session_state.prev_comment_count = next_save_threshold

        import math # mathモジュールをインポートしてください

        # ギフトログ自動保存
        prev_gift_count = st.session_state.get("prev_gift_count", 0)
        current_gift_count = len(st.session_state.gift_log)

        # 🌟 修正点1: 次に保存を実行すべき100の倍数を計算
        # 例: prev_gift_countが105の場合、next_save_thresholdは200になる
        next_save_threshold = math.ceil((prev_gift_count + 1) / 100) * 100

        # 🌟 修正点2: 条件判定を次の100の倍数に達したかどうかに変更
        if current_gift_count >= next_save_threshold:
            if current_gift_count > 0:
                gift_df = pd.DataFrame([
                    {
                        "ギフト時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                        "ユーザー名": log.get("name", ""),
                        "ギフト名": st.session_state.gift_list_map.get(str(log.get("gift_id")), {}).get("name", ""),
                        "個数": log.get("num", ""),
                        "ポイント": st.session_state.gift_list_map.get(str(log.get("gift_id")), {}).get("point", 0),
                        "ユーザーID": log.get("user_id", "")
                    }
                    for log in st.session_state.gift_log
                ])
                
                buf = io.BytesIO()
                gift_df.to_csv(buf, index=False, encoding="utf-8-sig")
                upload_csv_to_ftp(f"gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)
                
                # 🌟 修正点3: prev_gift_countを、実際に保存したときの総数ではなく、
                # 次の保存しきい値（100の倍数）に強制的に更新する
                st.session_state.prev_gift_count = next_save_threshold

        #auto_backup_if_needed()
        st.session_state.gift_list_map = get_gift_list(st.session_state.room_id)
        fan_list, total_fan_count = get_fan_list(st.session_state.room_id)
        st.session_state.fan_list = fan_list
        st.session_state.total_fan_count = total_fan_count

        # --- 無償ギフト・システムMSG：キューからデータを取り出してログに変換 ---
        while not gift_queue.empty():
            try:
                raw_data = gift_queue.get_nowait()
                
                # t の判定（文字列に変換して比較するのが最も安全です）
                m_type = str(raw_data.get("t", ""))

                # --- ✅ A. システムメッセージ (t: 18) の処理 ---
                if m_type == "18":
                    # time.time() は使わず、datetime で安全にタイムスタンプを取得
                    ts = raw_data.get("created_at") or int(datetime.datetime.now().timestamp())
                    new_sys_entry = {
                        "created_at": ts,
                        "message": raw_data.get("m", ""),
                        "user_id": raw_data.get("u")
                    }
                    st.session_state.system_msg_log.insert(0, new_sys_entry)
                    # st.session_state.system_msg_log = st.session_state.system_msg_log[:200]

                # --- 🎁 B. 無償ギフト (t: 2) の処理 ---
                elif m_type == "2":
                    g_id = raw_data.get("g")
                    if g_id is None:
                        continue
                    
                    # ギフトマスターとの照合
                    master = st.session_state.get("free_gift_master", {})
                    # IDが数値でも文字列でも見つけられるように検索
                    gift_info = master.get(str(g_id)) or master.get(g_id)
                    
                    if not gift_info:
                        # マスターにない（有償ギフトなど）場合はスキップ
                        continue
                    
                    ts = raw_data.get("created_at") or int(datetime.datetime.now().timestamp())
                    new_entry = {
                        "created_at": ts,
                        "user_id": raw_data.get("u"),
                        "name": raw_data.get("ac"),
                        "avatar_id": raw_data.get("av"),
                        "gift_id": str(g_id),
                        "gift_name": gift_info.get("name"),
                        "point": gift_info.get("point", 1),
                        "num": raw_data.get("n", 1),
                        "image": gift_info.get("image", "")
                    }
                    st.session_state.free_gift_log.insert(0, new_entry)
                    # st.session_state.free_gift_log = st.session_state.free_gift_log[:200]

            except Exception as e:
                # ここで print しておけば、アプリを止めずにコンソールで原因を確認できます
                print(f"Loop Error: {e}")
                continue

        # 最後に時間順にソート（念のため）
        st.session_state.free_gift_log.sort(key=lambda x: x["created_at"], reverse=True)

        # --- 無償ギフトログ自動保存 (100件ごと) ---
        prev_free_gift_count = st.session_state.get("prev_free_gift_count", 0)
        current_free_gift_count = len(st.session_state.free_gift_log)
        next_free_save_threshold = math.ceil((prev_free_gift_count + 1) / 100) * 100

        if current_free_gift_count >= next_free_save_threshold:
            if current_free_gift_count > 0:
                free_gift_df = pd.DataFrame([
                    {
                        "ギフト時間": datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%Y-%m-%d %H:%M:%S"),
                        "ユーザー名": log.get("name", ""),
                        "ギフト名": log.get("gift_name", ""),
                        "個数": log.get("num", ""),
                        "ポイント": log.get("point", 0),
                        "ユーザーID": log.get("user_id", "")
                    }
                    for log in st.session_state.free_gift_log
                ])
                buf = io.BytesIO()
                free_gift_df.to_csv(buf, index=False, encoding="utf-8-sig")
                upload_csv_to_ftp(f"free_gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv", buf)
                st.session_state.prev_free_gift_count = next_free_save_threshold

        st.markdown("---")
        st.markdown("<h2 style='font-size:2em;'>📊 リアルタイムダッシュボード</h2>", unsafe_allow_html=True)
        st.markdown(f"**最終更新日時 (日本時間): {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}**")
        st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※約10秒ごとに自動更新されます。</p>", unsafe_allow_html=True)

        # カラムを4つに分割
        col_comment, col_gift, col_free_gift, col_fan = st.columns(4)

        with col_comment:
            st.markdown("###### 📝 コメント")
            with st.container(border=True, height=500):
                filtered_comments = [
                    log for log in st.session_state.comment_log 
                    if not any(keyword in log.get('name', '') or keyword in log.get('comment', '') for keyword in SYSTEM_COMMENT_KEYWORDS)
                ]
                if filtered_comments:
                    # 💡 表示制限コントロール (制限したい場合は [:100] を有効にする)
                    display_comments = filtered_comments # [:100]
                    for log in display_comments:
                        user_name = log.get('name', '匿名ユーザー')
                        comment_text = log.get('comment', '')
                        created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                        avatar_url = log.get('avatar_url', '')
                        html = f"""
                        <div class="comment-item">
                            <div class="comment-item-row">
                                <img src="{avatar_url}" class="comment-avatar" />
                                <div class="comment-content">
                                    <div class="comment-time">{created_at}</div>
                                    <div class="comment-user">{user_name}</div>
                                    <div class="comment-text">{comment_text}</div>
                                </div>
                            </div>
                        </div>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 8px 0;">
                        """
                        st.markdown(html, unsafe_allow_html=True)
                else:
                    st.info("コメントはまだありません。")

        with col_gift:
            st.markdown("###### 🎁 スペシャルギフト")
            with st.container(border=True, height=500):
                if st.session_state.gift_log:
                    # 最新のキャッシュを取得
                    current_map = st.session_state.gift_list_map
                    display_gifts = st.session_state.gift_log
                    
                    for log in display_gifts:
                        gid = str(log.get('gift_id'))
                        
                        # --- 💡 未知のギフトID対策ロジック ---
                        if gid not in current_map:
                            # リストにないIDが来たら、その場でAPIを叩き直す
                            current_map = get_gift_list(st.session_state.room_id, force_update=True)
                        # ----------------------------------

                        gift_info = current_map.get(gid, {})
                        if not gift_info:
                            # それでも取得できない場合のフォールバック
                            gift_name = "未知のギフト"
                            gift_point = 0
                            gift_image_url = log.get('image', '')
                        else:
                            gift_name = gift_info.get('name', 'N/A')
                            gift_point = gift_info.get('point', 0)
                            gift_image_url = log.get('image', gift_info.get('image', ''))

                        user_name = log.get('name', '匿名ユーザー')
                        created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                        gift_count = log.get('num', 0)
                        total_point = gift_point * gift_count
                        
                        # 背景色の判定
                        highlight_class = ""
                        if total_point >= 300000: highlight_class = "highlight-300000"
                        elif total_point >= 100000: highlight_class = "highlight-100000"
                        elif total_point >= 60000: highlight_class = "highlight-60000"
                        elif total_point >= 30000: highlight_class = "highlight-30000"
                        elif total_point >= 1: highlight_class = "highlight-10000"
                        
                        avatar_id = log.get('avatar_id', None)
                        avatar_url = f"https://static.showroom-live.com/image/avatar/{avatar_id}.png" if avatar_id else DEFAULT_AVATAR
                        
                        html = f"""
                        <div class="gift-item {highlight_class}">
                            <div class="gift-item-row">
                                <img src="{avatar_url}" class="gift-avatar" />
                                <div class="gift-content">
                                    <div class="gift-time">{created_at}</div>
                                    <div class="gift-user">{user_name}</div>
                                    <div class="gift-info-row">
                                        <img src="{gift_image_url}" class="gift-image" title="{gift_name}" />
                                        <span>×{gift_count}</span>
                                    </div>
                                    <div style="font-size: 0.9em; color: #555;">{total_point} pt</div>
                                </div>
                            </div>
                        </div>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 8px 0;">
                        """
                        st.markdown(html, unsafe_allow_html=True)
                else:
                    st.info("スペシャルギフトはまだありません。")

        with col_free_gift:
            st.markdown("###### 🎈 無償ギフト")
            with st.container(border=True, height=500):
                if st.session_state.free_gift_log:
                    # 💡 表示制限コントロール
                    display_free_gifts = st.session_state.free_gift_log # [:100]
                    for log in display_free_gifts:
                        user_name = log.get('name', '匿名ユーザー')
                        created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                        gift_count = log.get('num', 0)
                        gift_point = log.get('point', 1) # 1pt
                        gift_image_url = log.get('image', '')
                        avatar_id = log.get('avatar_id', None)
                        avatar_url = f"https://static.showroom-live.com/image/avatar/{avatar_id}.png" if avatar_id else DEFAULT_AVATAR
                        
                        # デザインをスペシャルギフト(col_gift)と統一
                        html = f"""
                        <div class="gift-item">
                            <div class="gift-item-row">
                                <img src="{avatar_url}" class="gift-avatar" />
                                <div class="gift-content">
                                    <div class="gift-time">{created_at}</div>
                                    <div class="gift-user">{user_name}</div>
                                    <div class="gift-info-row">
                                        <img src="{gift_image_url}" class="gift-image" />
                                        <span>×{gift_count}</span>
                                    </div>
                                    <div>{gift_point} pt</div>
                                </div>
                            </div>
                        </div>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 8px 0;">
                        """
                        st.markdown(html, unsafe_allow_html=True)
                else:
                    st.info("無償ギフトはまだありません。")

        with col_fan:
            st.markdown("###### 🧡 システムMSG") 
            with st.container(border=True, height=500):
                if st.session_state.get("system_msg_log"):
                    for log in st.session_state.system_msg_log:
                        created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                        msg_text = log.get('message', '')
                        
                        # --- 💡 ハイライト判定ロジック（優先順位順） ---
                        bg_color = "transparent"
                        border_color = "transparent"

                        # 1. 〇〇回目の訪問 (最優先・最も目立つ)
                        if "回目の訪問" in msg_text:
                            bg_color = "#ffebee"  # 薄い赤（お祝い感）
                            # border_color = "#ffcdd2"
                        
                        # 2. 初訪問 (次に目立つ)
                        elif "初訪問" in msg_text:
                            bg_color = "#e3f2fd"  # 薄い青（フレッシュな印象）
                            # border_color = "#bbdefb"

                        # 3. 2度目の訪問
                        elif "2度目の訪問" in msg_text:
                            bg_color = "#f5f5f5"  # ごく薄いグレー
                            # border_color = "#eeeeee"

                        # 4. フォロー通知 (追加箇所)
                        elif "フォローしました" in msg_text:
                            bg_color = "#e8f5e9"  # 薄い緑（新規アクション感）
                            # border_color = "#f8bbd0"

                        # 5. ファンレベル上昇 (Lv10: 暖色 / Lv9: 同系統の薄い色)
                        elif "ファンレベルが10に" in msg_text:
                            bg_color = "#fff3cd"  # ゴールド（ファン化）
                            # border_color = "#ffeeba"
                        elif "ファンレベルが9に" in msg_text:
                            bg_color = "#fff9e6"  # さらに薄いイエロー（リーチ）
                            # border_color = "#fff3cd"
                        
                        # スタイルの組み立て
                        # style = f"background-color: {bg_color}; border: 1px solid {border_color}; padding: 0px 8px 4px 8px; border-radius: 4px; margin-bottom: 2px;"
                        style = f"background-color: {bg_color}; padding: 0px 8px 4px 8px; margin-bottom: 2px;"
                        
                        html = f"""
                        <div class="comment-item" style="{style}">
                            <div class="comment-time">{created_at}</div>
                            <div style="color: #FF6C1A; font-weight: bold; font-size: 0.9em; line-height: 1.5; margin-top: 2px;">
                                {msg_text}
                            </div>
                        </div>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 8px 0;">
                        """
                        st.markdown(html, unsafe_allow_html=True)
                else:
                    st.info("システムメッセージはありません。")
    else:
        st.warning("指定されたルームIDが見つからないか、認証されていないルームIDか、現在配信中ではありません。")
        st.session_state.is_tracking = False


# if st.session_state.is_tracking and st.session_state.room_id:
if st.session_state.get("room_id"):

    st.markdown("---")
    st.markdown("<h2 style='font-size:2em;'>📝 ログ詳細</h2>", unsafe_allow_html=True)
    
    # 統計情報の表示にシステムMSG件数を追加
    sys_msg_count = len(st.session_state.get("system_msg_log", []))
    st.markdown(
        f"<p style='font-size:12px; color:#a1a1a1;'>"
        f"※データは現在 {len(st.session_state.comment_log)} 件のコメント、"
        f"{len(st.session_state.gift_log)} 件のスペシャルギフト、"
        f"{len(st.session_state.free_gift_log)} 件の無償ギフト、"
        f"{sys_msg_count} 件のシステムMSG、" # 追加
        f"および {st.session_state.total_fan_count} 名のファンのデータが蓄積されています。<br />"
        f"※誤ってリロード（再読み込み）してしまった、閉じてしまった等でダウンロードせずに消失してしまった場合、"
        f"運営にご相談いただければ、ログ取得できる可能性があります。<br />"
        f"※各タブを選択し、必要に応じて「＞」で詳細を展開してください。</p>", 
        unsafe_allow_html=True
    )

    # --- タブの作成 (タブ名を変更) ---
    tab_com, tab_sp, tab_free, tab_all, tab_fan = st.tabs([
        "💬🧡 コメント&MSG", "🎁 スペシャルギフト", "🎈 無償ギフト", "🎁🎈 ギフト統合 (SP&無償)", "🏆 ファンリスト"
    ])

    # ==========================================
    # タブ1: コメント & システムメッセージログ
    # ==========================================
    with tab_com:
        # --- 1. コメントログ部分 ---
        with st.expander("📝 コメントログ一覧", expanded=True):
            filtered_comments = [
                log for log in st.session_state.comment_log 
                if not any(keyword in log.get('name', '') or keyword in log.get('comment', '') for keyword in SYSTEM_COMMENT_KEYWORDS)
            ]
            if filtered_comments:
                c_df = pd.DataFrame(filtered_comments)
                c_df['コメント時間'] = pd.to_datetime(c_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                c_df = c_df.rename(columns={'name': 'ユーザー名', 'comment': 'コメント内容', 'user_id': 'ユーザーID'})
                
                st.dataframe(c_df[['コメント時間', 'ユーザー名', 'コメント内容']], use_container_width=True, hide_index=True)
                
                buf_com = io.BytesIO()
                c_df[['コメント時間', 'ユーザー名', 'ユーザーID', 'コメント内容']].to_csv(buf_com, index=False, encoding='utf-8-sig')
                st.download_button("コメントログをダウンロード", buf_com.getvalue(), f"comment_log_{st.session_state.room_id}.csv", "text/csv", key="dl_c")
            else:
                st.info("コメントデータがありません。")

        # --- 2. システムMSGログ部分 (追加) ---
        with st.expander("🧡 システムMSGログ一覧", expanded=True):
            system_msgs = st.session_state.get("system_msg_log", [])
            if system_msgs:
                s_msg_df = pd.DataFrame(system_msgs)
                # 表示時間の変換
                s_msg_df['表示時間'] = pd.to_datetime(s_msg_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                # カラム名の整理
                s_msg_df = s_msg_df.rename(columns={'message': '表示内容'})
                
                # 表示用データフレーム（CSVダウンロード不要とのことなので表示のみ）
                st.dataframe(s_msg_df[['表示時間', '表示内容']], use_container_width=True, hide_index=True)
            else:
                st.info("システムメッセージデータがありません。")

    # ==========================================
    # タブ2: スペシャルギフトログ
    # ==========================================
    with tab_sp:
        if st.session_state.gift_log:
            s_raw = pd.DataFrame(st.session_state.gift_log)
            if st.session_state.gift_list_map:
                g_info = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
                g_info.index = g_info.index.astype(str)
                s_raw['gift_id'] = s_raw['gift_id'].astype(str)
                s_raw = s_raw.set_index('gift_id').join(g_info, on='gift_id', lsuffix='_u', rsuffix='_g').reset_index()

            # 1. 全量一覧
            with st.expander("📜 スペシャルギフトログ一覧表 (全量)", expanded=True):
                s_disp = s_raw.copy()
                s_disp['ギフト時間'] = pd.to_datetime(s_disp['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                s_disp['合計Pt（※単純合計値）'] = (pd.to_numeric(s_disp['num']) * pd.to_numeric(s_disp['point'])).astype(int)
                s_disp = s_disp.rename(columns={'name_u': 'ユーザー名', 'name_g': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'user_id': 'ユーザーID'})
                st.dataframe(s_disp[['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)
                
                buf_s1 = io.BytesIO()
                s_disp[['ギフト時間', 'ユーザー名', 'ユーザーID', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']].to_csv(buf_s1, index=False, encoding='utf-8-sig')
                st.download_button("スペシャルギフトログをダウンロード", buf_s1.getvalue(), "sp_gift_all.csv", "text/csv", key="dl_s1")

            # 2. ギフト単位合算
            with st.expander("🎁 ユーザー単位でギフト合算集計", expanded=False):
                s_sum = s_raw.groupby(['user_id', 'name_g', 'point'], as_index=False).agg({'num': 'sum', 'created_at': 'max', 'name_u': 'last'})
                s_sum['合計Pt（※単純合計値）'] = (s_sum['num'] * pd.to_numeric(s_sum['point'])).astype(int)
                s_sum['最新ギフト時間'] = pd.to_datetime(s_sum['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                s_sum = s_sum.rename(columns={'name_u': 'ユーザー名', 'name_g': 'ギフト名', 'num': '合計個数', 'point': 'ポイント', 'user_id': 'ユーザーID'}).sort_values('最新ギフト時間', ascending=False)
                st.dataframe(s_sum[['最新ギフト時間', 'ユーザー名', 'ギフト名', '合計個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)

            # 3. ユーザー単位集計 (貢献順)
            with st.expander("👤 ユーザー単位で集計 (総貢献Pt順)", expanded=False):
                s_user = s_raw.copy()
                s_user['line_pt'] = pd.to_numeric(s_user['num']) * pd.to_numeric(s_user['point'])
                latest_names = s_user.sort_values('created_at').groupby('user_id')['name_u'].last()
                u_agg = s_user.groupby(['user_id', 'name_g', 'point'], as_index=False).agg({'num': 'sum', 'line_pt': 'sum'})
                u_total = u_agg.groupby('user_id')['line_pt'].sum().rename('総Pt')
                u_merged = u_agg.merge(u_total, on='user_id').sort_values(['総Pt', 'user_id', 'line_pt'], ascending=[False, True, False])
                
                u_rows = []
                prev_id = None
                for _, r in u_merged.iterrows():
                    u_rows.append({
                        'ユーザー名': latest_names[r['user_id']] if r['user_id'] != prev_id else '',
                        'ギフト名': r['name_g'], '合計個数': r['num'], 'ポイント': r['point'],
                        'ギフト単位Pt': int(r['line_pt']), '総貢献Pt（※単純合計値）': int(r['総Pt']) if r['user_id'] != prev_id else ''
                    })
                    prev_id = r['user_id']
                st.dataframe(pd.DataFrame(u_rows), use_container_width=True, hide_index=True)
        else:
            st.info("スペシャルギフトデータがありません。")

    # ==========================================
    # タブ3: 無償ギフトログ
    # ==========================================
    with tab_free:
        if st.session_state.free_gift_log:
            f_raw = pd.DataFrame(st.session_state.free_gift_log)
            
            with st.expander("📜 無償ギフトログ一覧表 (全量)", expanded=True):
                f_disp = f_raw.copy()
                f_disp['ギフト時間'] = pd.to_datetime(f_disp['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                f_disp['合計Pt（※単純合計値）'] = (pd.to_numeric(f_disp['num']) * pd.to_numeric(f_disp['point'])).astype(int)
                f_disp = f_disp.rename(columns={'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'user_id': 'ユーザーID'})
                st.dataframe(f_disp[['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)
                
                buf_f1 = io.BytesIO()
                f_disp[['ギフト時間', 'ユーザー名', 'ユーザーID', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']].to_csv(buf_f1, index=False, encoding='utf-8-sig')
                st.download_button("無償ギフトログをダウンロード", buf_f1.getvalue(), "free_gift_all.csv", "text/csv", key="dl_f1")

            with st.expander("🎈 ユーザー単位でギフト合算集計", expanded=False):
                f_sum = f_raw.groupby(['user_id', 'gift_name', 'point'], as_index=False).agg({'num': 'sum', 'created_at': 'max', 'name': 'last'})
                f_sum['合計Pt（※単純合計値）'] = (f_sum['num'] * pd.to_numeric(f_sum['point'])).astype(int)
                f_sum['最新ギフト時間'] = pd.to_datetime(f_sum['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                f_sum = f_sum.rename(columns={'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '合計個数', 'point': 'ポイント'}).sort_values('最新ギフト時間', ascending=False)
                st.dataframe(f_sum[['最新ギフト時間', 'ユーザー名', 'ギフト名', '合計個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)

            with st.expander("👤 ユーザー単位で集計 (総貢献Pt順)", expanded=False):
                f_u_df = f_raw.copy()
                f_u_df['line_pt'] = pd.to_numeric(f_u_df['num']) * pd.to_numeric(f_u_df['point'])
                latest_f_names = f_u_df.sort_values('created_at').groupby('user_id')['name'].last()
                f_u_agg = f_u_df.groupby(['user_id', 'gift_name', 'point'], as_index=False).agg({'num': 'sum', 'line_pt': 'sum'})
                f_u_total = f_u_agg.groupby('user_id')['line_pt'].sum().rename('総Pt')
                f_u_merged = f_u_agg.merge(f_u_total, on='user_id').sort_values(['総Pt', 'user_id', 'line_pt'], ascending=[False, True, False])
                
                f_u_rows = []
                prev_f_id = None
                for _, r in f_u_merged.iterrows():
                    f_u_rows.append({
                        'ユーザー名': latest_f_names[r['user_id']] if r['user_id'] != prev_f_id else '',
                        'ギフト名': r['gift_name'], '合計個数': r['num'], 'ポイント': r['point'],
                        'ギフト単位Pt': int(r['line_pt']), '総貢献Pt（※単純合計値）': int(r['総Pt']) if r['user_id'] != prev_f_id else ''
                    })
                    prev_f_id = r['user_id']
                st.dataframe(pd.DataFrame(f_u_rows), use_container_width=True, hide_index=True)
        else:
            st.info("無償ギフトデータがありません。")

    # ==========================================
    # タブ4: スペシャル＆無償 統合ログ
    # ==========================================
   
    with tab_all:
        combined_data = []
        if st.session_state.gift_log:
            s_part = pd.DataFrame(st.session_state.gift_log)
            if st.session_state.gift_list_map:
                g_map = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
                s_part['gift_id'] = s_part['gift_id'].astype(str)
                s_part = s_part.set_index('gift_id').join(g_map, on='gift_id', lsuffix='_u', rsuffix='_g').reset_index()
                s_part = s_part.rename(columns={'name_u': 'name', 'name_g': 'gift_name'})
            combined_data.append(s_part[['created_at', 'name', 'user_id', 'gift_name', 'num', 'point']])
        
        if st.session_state.free_gift_log:
            f_part = pd.DataFrame(st.session_state.free_gift_log)
            combined_data.append(f_part[['created_at', 'name', 'user_id', 'gift_name', 'num', 'point']])

        if combined_data:
            all_df = pd.concat(combined_data, ignore_index=True)
            all_df['created_at_dt'] = pd.to_datetime(all_df['created_at'], unit='s')

            with st.expander("📜 SP&無償ギフトログ一覧表 (全量)", expanded=True):
                all_disp = all_df.sort_values('created_at', ascending=False).copy()
                all_disp['ギフト時間'] = all_disp['created_at_dt'].dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                all_disp['合計Pt（※単純合計値）'] = (pd.to_numeric(all_disp['num']) * pd.to_numeric(all_disp['point'])).astype(int)
                all_disp = all_disp.rename(columns={'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'user_id': 'ユーザーID'})
                st.dataframe(all_disp[['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)
                
                buf_all1 = io.BytesIO()
                all_disp[['ギフト時間', 'ユーザー名', 'ユーザーID', 'ギフト名', '個数', 'ポイント', '合計Pt（※単純合計値）']].to_csv(buf_all1, index=False, encoding='utf-8-sig')
                st.download_button("SP&無償ギフトログをダウンロード", buf_all1.getvalue(), "combined_gift_all.csv", "text/csv", key="dl_all1")

            with st.expander("🎁🎈 ユーザー単位でギフト合算集計", expanded=False):
                all_sum = all_df.groupby(['user_id', 'gift_name', 'point'], as_index=False).agg({'num': 'sum', 'created_at_dt': 'max', 'name': 'last'})
                all_sum['合計Pt（※単純合計値）'] = (all_sum['num'] * pd.to_numeric(all_sum['point'])).astype(int)
                all_sum['最新ギフト時間'] = all_sum['created_at_dt'].dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                all_sum = all_sum.rename(columns={'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '合計個数', 'point': 'ポイント'}).sort_values('最新ギフト時間', ascending=False)
                st.dataframe(all_sum[['最新ギフト時間', 'ユーザー名', 'ギフト名', '合計個数', 'ポイント', '合計Pt（※単純合計値）']], use_container_width=True, hide_index=True)

            with st.expander("👤 ユーザー単位で集計 (総貢献Pt順)", expanded=False):
                all_u = all_df.copy()
                all_u['line_pt'] = pd.to_numeric(all_u['num']) * pd.to_numeric(all_u['point'])
                latest_all_names = all_u.sort_values('created_at').groupby('user_id')['name'].last()
                all_u_agg = all_u.groupby(['user_id', 'gift_name', 'point'], as_index=False).agg({'num': 'sum', 'line_pt': 'sum'})
                all_u_total = all_u_agg.groupby('user_id')['line_pt'].sum().rename('総Pt')
                all_u_merged = all_u_agg.merge(all_u_total, on='user_id').sort_values(['総Pt', 'user_id', 'line_pt'], ascending=[False, True, False])
                
                all_u_rows = []
                prev_all_id = None
                for _, r in all_u_merged.iterrows():
                    all_u_rows.append({
                        'ユーザー名': latest_all_names[r['user_id']] if r['user_id'] != prev_all_id else '',
                        'ギフト名': r['gift_name'], '合計個数': r['num'], 'ポイント': r['point'],
                        'ギフト単位Pt': int(r['line_pt']), '総貢献Pt（※単純合計値）': int(r['総Pt']) if r['user_id'] != prev_all_id else ''
                    })
                    prev_all_id = r['user_id']
                st.dataframe(pd.DataFrame(all_u_rows), use_container_width=True, hide_index=True)
        else:
            st.info("SP&無償ギフトデータがありません。")

    # ==========================================
    # タブ5: ファンリスト
    # ==========================================
    with tab_fan:
        if st.session_state.fan_list:
            raw_fan_df = pd.DataFrame(st.session_state.fan_list)
            rename_map = {'rank': '順位', 'level': 'レベル', 'user_name': 'ユーザー名', 'point': 'ポイント', 'user_id': 'ユーザーID'}
            existing_rename_map = {k: v for k, v in rename_map.items() if k in raw_fan_df.columns}
            fan_df = raw_fan_df.rename(columns=existing_rename_map)
            desired_cols = ['順位', 'レベル', 'ユーザー名', 'ポイント', 'ユーザーID']
            final_display_cols = [c for c in desired_cols if c in fan_df.columns]
            
            st.markdown("### 🏆 ファンリスト一覧")
            st.dataframe(fan_df[final_display_cols], use_container_width=True, hide_index=True)
            
            buf_fan = io.BytesIO()
            fan_df[final_display_cols].to_csv(buf_fan, index=False, encoding='utf-8-sig')
            st.download_button(label="ファンリストをダウンロード", data=buf_fan.getvalue(), file_name=f"fan_list_{st.session_state.room_id}.csv", mime="text/csv", key="dl_f_final")
        else:
            st.info("ファンデータがありません。")