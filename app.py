import streamlit as st
import requests
import pandas as pd
import pytz
import datetime
import io
from streamlit_autorefresh import st_autorefresh
import ftplib
import io
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

def get_gift_list(room_id):
    if st.session_state.gift_list_map:
        return st.session_state.gift_list_map
    url = f"{GIFT_LIST_API_URL}?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        gift_list_map = {}
        for gift in data.get('normal', []) + data.get('special', []):
            try:
                point_value = int(gift.get('point', 0))
            except (ValueError, TypeError):
                point_value = 0
            gift_list_map[str(gift['gift_id'])] = {
                'name': gift.get('gift_name', 'N/A'),
                'point': point_value,
                'image': gift.get('image', '')
            }
        st.session_state.gift_list_map = gift_list_map
        return gift_list_map
    except requests.exceptions.RequestException as e:
        st.error(f"ルームID {room_id} のギフトリスト取得中にエラーが発生しました: {e}")
        return {}


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
        if not st.session_state.get("is_master_access", False) and input_room_id not in valid_ids:
            st.error("指定されたルームIDが見つからないか、認証されていないルームIDか、現在配信中ではありません。")
        else:
            # --- 基本設定 ---
            st.session_state.is_tracking = True
            st.session_state.room_id = input_room_id
            
            # --- 既存ログの初期化 ---
            st.session_state.comment_log = []
            st.session_state.gift_log = []
            st.session_state.gift_list_map = {}
            st.session_state.fan_list = []
            st.session_state.total_fan_count = 0
            
            # --- 新設：無償ギフト用の初期化 ---
            st.session_state.free_gift_log = []
            st.session_state.raw_free_gift_queue = []
            
            # 1. 無償ギフトマスター（名前・画像・ポイント）の取得
            update_free_gift_master(input_room_id) # ← コメント解除
            
            # 2. WebSocket接続情報の取得
            streaming_info = get_streaming_server_info(input_room_id) # ← コメント解除
            
            if streaming_info:
                # 3. 既存の受信機が動いていれば停止
                if st.session_state.get("ws_receiver"):
                    try:
                        st.session_state.ws_receiver.stop()
                    except:
                        pass
                
                # 4. 無償ギフト受信機（WebSocket）をバックグラウンドで起動
                # 引数名を free_gift_handler.py の定義（host, key）に合わせて修正します
                receiver = FreeGiftReceiver(
                    room_id=input_room_id,
                    host=streaming_info["host"],  # bcsvr_host から host に変更
                    key=streaming_info["key"]    # bcsvr_key から key に変更
                )
                receiver.start()
                st.session_state.ws_receiver = receiver
            else:
                st.warning("配信サーバー情報の取得に失敗したため、無償ギフトのリアルタイム取得はスキップされます。")

            st.rerun()
    else:
        st.error("ルームIDを入力してください。")

if st.button("トラッキング停止", key="stop_button", disabled=not st.session_state.is_tracking):
    if st.session_state.is_tracking:
        # 保存対象に無償ギフトを追加
        save_log_to_ftp("comment")
        save_log_to_ftp("gift")
        save_log_to_ftp("free_gift")

    st.session_state.is_tracking = False
    st.session_state.room_info = None
    st.info("トラッキングを停止しました。")
    st.rerun()


if st.session_state.is_tracking:
    onlives_data = get_onlives_rooms()
    target_room_info = onlives_data.get(int(st.session_state.room_id)) if st.session_state.room_id.isdigit() else None

    # --- 配信終了検知と自動保存処理 ---
    # インデントを一段（半角スペース4つ）に統一しています
    is_live_now = int(st.session_state.room_id) in onlives_data

    if not is_live_now:
        st.warning("📡 配信が終了しました。全ログを最終保存します。")

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

        # 状態変更とリロード
        st.session_state.is_tracking = False
        st.info("✅ 配信終了を検知し、すべてのログを保存しました。トラッキングを停止します。")
        st.rerun()


    if target_room_info:
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
        st.markdown(f'<div class="tracking-success">{link_html} の配信をトラッキング中です！</div>', unsafe_allow_html=True)

        st_autorefresh(interval=10000, limit=None, key="dashboard_refresh")
        st.session_state.comment_log = get_and_update_log("comment", st.session_state.room_id)
        st.session_state.gift_log = get_and_update_log("gift", st.session_state.room_id)
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

        # --- 無償ギフト：キューからデータを取り出してログに変換 ---
        import time
        while not gift_queue.empty():
            try:
                raw_data = gift_queue.get_nowait()
                gift_id = raw_data.get("g")
                
                # 💡 ここが重要：マスター（1ptのギフトだけが入っている辞書）に
                # 存在しないギフトID（20ptなど）は、このループでは処理せず無視（continue）する
                # これにより、20ptは「スペシャルギフト」側にのみ表示されるようになります
                master = st.session_state.get("free_gift_master", {}).get(gift_id)
                if not master:
                    continue
                
                master = st.session_state.free_gift_master[gift_id]
                
                new_entry = {
                    "created_at": raw_data.get("created_at", int(time.time())),
                    "user_id": raw_data.get("u"),
                    "name": raw_data.get("ac"),
                    "avatar_id": raw_data.get("av"),
                    "gift_id": gift_id,
                    "gift_name": master.get("name"),
                    "point": master.get("point", 1),
                    "num": raw_data.get("n", 1),
                    "image": master.get("image", "")
                }
                
                # ログの先頭に追加（新しい順）
                st.session_state.free_gift_log.insert(0, new_entry)
                
                # ログが溜まりすぎないよう制限（直近100件までなど）
                # if len(st.session_state.free_gift_log) > 100:
                #     st.session_state.free_gift_log = st.session_state.free_gift_log[:100]
                    
            except Exception as e:
                break
            
            # 新しい順にソート
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
            st.markdown("##### 📝 コメント")
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
            st.markdown("##### 🎁 スペシャルギフト")
            with st.container(border=True, height=500):
                if st.session_state.gift_log and st.session_state.gift_list_map:
                    # 💡 表示制限コントロール
                    display_gifts = st.session_state.gift_log # [:100]
                    for log in display_gifts:
                        gift_info = st.session_state.gift_list_map.get(str(log.get('gift_id')), {})
                        if not gift_info:
                            continue
                        user_name = log.get('name', '匿名ユーザー')
                        created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                        gift_point = gift_info.get('point', 0)
                        gift_count = log.get('num', 0)
                        total_point = gift_point * gift_count
                        
                        highlight_class = ""
                        if total_point >= 300000: highlight_class = "highlight-300000"
                        elif total_point >= 100000: highlight_class = "highlight-100000"
                        elif total_point >= 60000: highlight_class = "highlight-60000"
                        elif total_point >= 30000: highlight_class = "highlight-30000"
                        elif total_point >= 10000: highlight_class = "highlight-10000"
                        
                        gift_image_url = log.get('image', gift_info.get('image', ''))
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
                    st.info("スペシャルギフトはまだありません。")

        with col_free_gift:
            st.markdown("##### 🎈 無償ギフト")
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
            st.markdown("##### 🏆 ファンリスト")
            with st.container(border=True, height=500):
                if st.session_state.fan_list:
                    display_fans = st.session_state.fan_list
                    for fan in display_fans:
                        # 他のカラム（comment-item等）と全く同じクラス構成に変更
                        html = f"""
                        <div class="fan-item">
                            <div class="fan-info-row">
                                <img src="https://static.showroom-live.com/image/avatar/{fan.get('avatar_id', 0)}.png?v=108" class="fan-avatar" />
                                <div class="fan-content">
                                    <div class="fan-level">Lv. {fan.get('level', 0)}</div>
                                    <div class="fan-user">{fan.get('user_name', '不明なユーザー')}</div>
                                </div>
                            </div>
                        </div>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 8px 0;">
                        """
                        st.markdown(html, unsafe_allow_html=True)
                else:
                    st.info("ファンデータがありません。")
    else:
        st.warning("指定されたルームIDが見つからないか、認証されていないルームIDか、現在配信中ではありません。")
        st.session_state.is_tracking = False

st.markdown("---")
st.markdown("<h2 style='font-size:2em;'>📝 ログ詳細</h2>", unsafe_allow_html=True)
st.markdown(
    f"<p style='font-size:12px; color:#a1a1a1;'>"
    f"※データは現在{len(st.session_state.comment_log)}件のコメントと、"
    f"{len(st.session_state.gift_log)}件のスペシャルギフト、"
    f"{len(st.session_state.free_gift_log)}件の無償ギフト、"
    f"および{st.session_state.total_fan_count}名のファンのデータが蓄積されています。<br />"
    f"※誤ってリロード（再読み込み）してしまった、閉じてしまった等でダウンロードせずに消失してしまった場合、"
    f"24時間以内に運営ご相談いただければ、復元・ログ取得できる可能性があります。</p>", 
    unsafe_allow_html=True
)
#st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※誤ってリロード（再読み込み）してしまった、閉じてしまった等でダウンロードせずに消失してしまった場合、24時間以内に運営ご相談いただければ、復元・ログ取得できる可能性があります。</p>", unsafe_allow_html=True)
st.markdown("")

comment_cols = ['コメント時間', 'ユーザー名', 'コメント内容', 'ユーザーID']
gift_cols = ['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']
fan_cols = ['順位', 'レベル', 'ユーザー名', 'ポイント', 'ユーザーID']

# コメント一覧表
filtered_comments_df = [
    log for log in st.session_state.comment_log 
    if not any(keyword in log.get('name', '') or keyword in log.get('comment', '') for keyword in SYSTEM_COMMENT_KEYWORDS)
]
if filtered_comments_df:
    comment_df = pd.DataFrame(filtered_comments_df)
    comment_df['created_at'] = pd.to_datetime(comment_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
    comment_df['user_id'] = [log.get('user_id', 'N/A') for log in filtered_comments_df]
    comment_df = comment_df.rename(columns={
        'name': 'ユーザー名', 'comment': 'コメント内容', 'created_at': 'コメント時間', 'user_id': 'ユーザーID'
    })
    st.markdown("#### 📝 コメントログ一覧表")
    st.dataframe(comment_df[comment_cols], use_container_width=True, hide_index=True)
    
    buffer = io.BytesIO()
    comment_df[comment_cols].to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)
    st.download_button(
        label="コメントログをCSVでダウンロード",
        data=buffer,
        file_name=f"comment_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )
else:
    st.info("ダウンロードできるコメントがありません。")

st.markdown("---")

# スペシャルギフトログ一覧表
if st.session_state.gift_log:
    gift_df = pd.DataFrame(st.session_state.gift_log)
    gift_df['created_at'] = pd.to_datetime(gift_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
    
    if st.session_state.gift_list_map:
        gift_info_df = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
        gift_info_df.index = gift_info_df.index.astype(str)
        
        gift_df['gift_id'] = gift_df['gift_id'].astype(str)
        gift_df = gift_df.set_index('gift_id').join(gift_info_df, on='gift_id', lsuffix='_user_data', rsuffix='_gift_info').reset_index()

    gift_df = gift_df.rename(columns={
        'name_user_data': 'ユーザー名', 'name_gift_info': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'created_at': 'ギフト時間', 'user_id': 'ユーザーID'
    })
    st.markdown("#### 🎁 スペシャルギフトログ一覧表")
    st.dataframe(gift_df[gift_cols], use_container_width=True, hide_index=True)
    
    buffer = io.BytesIO()
    gift_df[gift_cols].to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)
    st.download_button(
        label="スペシャルギフトログをCSVでダウンロード",
        data=buffer,
        file_name=f"gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )
else:
    st.info("ダウンロードできるスペシャルギフトがありません。")

st.markdown("---")

# ▼▼▼ スペシャルギフトログ一覧表 （ユーザー単位で集計） [名前変更対策版] ▼▼▼

if st.session_state.gift_log:
    gift_df2 = pd.DataFrame(st.session_state.gift_log)

    # created_at の変換（最新の名前を特定するために使用）
    gift_df2['created_at_dt'] = pd.to_datetime(gift_df2['created_at'], unit='s')

    # ギフト名・ポイントを gift_list_map から補完
    gift_df2['gift_id'] = gift_df2['gift_id'].astype(str)
    if st.session_state.gift_list_map:
        gift_info_df2 = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
        gift_info_df2.index = gift_info_df2.index.astype(str)

        gift_df2 = (
            gift_df2.set_index('gift_id')
                    .join(gift_info_df2, on='gift_id',
                          lsuffix='_user_data', rsuffix='_gift_info')
                    .reset_index()
        )

    # カラム整形
    gift_df2 = gift_df2.rename(columns={
        'name_user_data': 'ユーザー名',
        'name_gift_info': 'ギフト名',
        'num': '個数',
        'point': 'ポイント',
        'user_id': 'ユーザーID'
    })

    # ---- 名前変更対策ロジック ----
    
    # 1. 各ユーザーIDに対して、一番最後に現れた（最新の）ユーザー名を取得
    latest_names = gift_df2.sort_values('created_at_dt').groupby('ユーザーID')['ユーザー名'].last().to_dict()

    # 2. 集計処理：ユーザー名ではなく「ユーザーID」を主軸に集計
    grouped = (
        gift_df2.groupby(['ユーザーID', 'ギフト名', 'ポイント'], as_index=False)
                .agg({'個数': 'sum'})
    )

    # 3. 集計結果に「最新のユーザー名」を紐付ける
    grouped['ユーザー名'] = grouped['ユーザーID'].map(latest_names)

    # ユーザーごとの合計ポイントを計算
    grouped['合計ポイント'] = grouped['個数'] * grouped['ポイント']

    # ユーザー単位の総ポイントを「ユーザーID」基準で集計
    user_total = grouped.groupby('ユーザーID')['合計ポイント'].sum().reset_index()
    user_total = user_total.rename(columns={'合計ポイント': 'ユーザー総ポイント'})

    # 総ポイントをマージ
    grouped = grouped.merge(user_total, on='ユーザーID', how='left')

    # ソート：
    # 1) ユーザー総ポイント（降順）
    # 2) ユーザーID（同一ユーザーのデータを固める）
    # 3) ギフトポイント（降順）
    grouped_sorted = grouped.sort_values(
        by=['ユーザー総ポイント', 'ユーザーID', 'ポイント'],
        ascending=[False, True, False]
    )

    # 表示用データの作成（ユーザーIDが変わったタイミングで名前を表示）
    display_rows = []
    prev_user_id = None
    for _, row in grouped_sorted.iterrows():
        curr_user_id = row['ユーザーID']
        display_rows.append({
            'ユーザー名': row['ユーザー名'] if curr_user_id != prev_user_id else '',
            'ギフト名': row['ギフト名'],
            '個数（合計）': row['個数'],
            'ポイント': row['ポイント']
        })
        prev_user_id = curr_user_id

    final_user_gift_df = pd.DataFrame(display_rows)

    # UI表示
    st.markdown(
        """
        <h3 style="font-size:1.5em; margin-bottom:6px;">
            🎁 スペシャルギフトログ一覧表
            <span style="font-size:0.7em; opacity:0.8;">（ユーザー単位で集計）</span>
        </h3>
        """,
        unsafe_allow_html=True
    )
    st.dataframe(final_user_gift_df, use_container_width=True, hide_index=True)

# ▲▲▲ ここまで ▲▲▲

# --- 無償ギフトログ一覧表 の追加 ---

st.markdown("---")

# 無償ギフト用のカラム定義
free_gift_cols = ['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']

if st.session_state.free_gift_log:
    # 1. ログのデータフレーム化
    free_gift_df = pd.DataFrame(st.session_state.free_gift_log)
    
    # 2. 時間をJSTに変換
    free_gift_df['created_at'] = pd.to_datetime(
        free_gift_df['created_at'], unit='s'
    ).dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
    
    # 3. カラム名の日本語化
    # ※ free_gift_log は既に gift_name や point を持っている構造なので join は不要です
    free_gift_df = free_gift_df.rename(columns={
        'name': 'ユーザー名', 
        'gift_name': 'ギフト名', 
        'num': '個数', 
        'point': 'ポイント', 
        'created_at': 'ギフト時間', 
        'user_id': 'ユーザーID'
    })
    
    st.markdown("#### 🎈 無償ギフトログ一覧表")
    st.dataframe(free_gift_df[free_gift_cols], use_container_width=True, hide_index=True)
    
    # 4. CSVダウンロードボタン
    buffer = io.BytesIO()
    free_gift_df[free_gift_cols].to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)
    st.download_button(
        label="無償ギフトログをCSVでダウンロード",
        data=buffer,
        file_name=f"free_gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="download_free_gift_csv" # 重複を避けるためのキー
    )
else:
    st.info("ダウンロードできる無償ギフトがありません。")

# --- 無償ギフトログ一覧表 ここまで ---

st.markdown("---")

# --- ▼▼▼ 無償ギフトログ一覧表 （ユーザー単位で集計） [名前変更対策版] ▼▼▼ ---

if st.session_state.free_gift_log:
    # 1. データの準備
    free_gift_df2 = pd.DataFrame(st.session_state.free_gift_log)

    # 時間順にソートするための準備（最新の名前を特定するため）
    free_gift_df2['created_at_dt'] = pd.to_datetime(free_gift_df2['created_at'], unit='s')

    # 2. カラム名の整理
    free_gift_df2 = free_gift_df2.rename(columns={
        'name': 'ユーザー名',
        'gift_name': 'ギフト名',
        'num': '個数',
        'point': 'ポイント',
        'user_id': 'ユーザーID'
    })

    # ---- 名前変更対策ロジック ----

    # A. 各ユーザーIDに対して、最新のユーザー名を取得して辞書にする
    latest_free_names = free_gift_df2.sort_values('created_at_dt').groupby('ユーザーID')['ユーザー名'].last().to_dict()

    # B. 集計処理：ユーザーIDを軸にして、ギフトごとの個数を合計
    free_grouped = (
        free_gift_df2.groupby(['ユーザーID', 'ギフト名', 'ポイント'], as_index=False)
                     .agg({'個数': 'sum'})
    )

    # C. 集計結果に最新のユーザー名をマッピング
    free_grouped['ユーザー名'] = free_grouped['ユーザーID'].map(latest_free_names)

    # 4. ソート用の計算（ユーザーごとの総個数を算出：ユーザーID基準）
    free_user_total = free_grouped.groupby('ユーザーID')['個数'].sum().reset_index()
    free_user_total = free_user_total.rename(columns={'個数': 'ユーザー総個数'})
    
    free_grouped = free_grouped.merge(free_user_total, on='ユーザーID', how='left')

    # 5. ソート実行
    # ユーザー総個数（降順） > ユーザーID（固定） > 各ギフト個数（降順）
    free_grouped_sorted = free_grouped.sort_values(
        by=['ユーザー総個数', 'ユーザーID', '個数'],
        ascending=[False, True, False]
    )

    # 6. 表示用データの作成（ユーザーIDが変わった時だけ名前を表示し、以降は空白にする）
    free_display_rows = []
    prev_user_id = None
    for _, row in free_grouped_sorted.iterrows():
        curr_user_id = row['ユーザーID']
        free_display_rows.append({
            'ユーザー名': row['ユーザー名'] if curr_user_id != prev_user_id else '',
            'ギフト名': row['ギフト名'],
            '個数（合計）': row['個数'],
            'ポイント': row['ポイント']
        })
        prev_user_id = curr_user_id

    final_user_free_gift_df = pd.DataFrame(free_display_rows)

    # 7. UI表示
    st.markdown(
        """
        <h3 style="font-size:1.5em; margin-bottom:6px; margin-top:20px;">
            🎈 無償ギフトログ一覧表
            <span style="font-size:0.7em; opacity:0.8;">（ユーザー単位で集計）</span>
        </h3>
        """,
        unsafe_allow_html=True
    )
    st.dataframe(final_user_free_gift_df, use_container_width=True, hide_index=True)

# --- ▲▲▲ 無償ギフト集計版 ここまで ▲▲▲ ---

# --- ▼▼▼ スペシャル＆無償ギフトログ一覧表（マージ版） ▼▼▼ ---

st.markdown("---")
st.markdown("#### 🎁🎈 スペシャル＆無償ギフトログ一覧表")

combined_list = []

# 1. スペシャルギフトの準備（既存のロジックで整形）
if st.session_state.gift_log:
    s_df = pd.DataFrame(st.session_state.gift_log)
    if st.session_state.gift_list_map:
        gift_info_df = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
        gift_info_df.index = gift_info_df.index.astype(str)
        s_df['gift_id'] = s_df['gift_id'].astype(str)
        s_df = s_df.set_index('gift_id').join(gift_info_df, on='gift_id', lsuffix='_u', rsuffix='_g').reset_index()
        
        # マージ用にカラム名を統一
        s_df = s_df.rename(columns={
            'name_u': 'ユーザー名', 'name_g': 'ギフト名', 'num': '個数', 
            'point': 'ポイント', 'created_at': 'raw_time', 'user_id': 'ユーザーID'
        })
        combined_list.append(s_df[['raw_time', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']])

# 2. 無償ギフトの準備（既存のロジックで整形）
if st.session_state.free_gift_log:
    f_df = pd.DataFrame(st.session_state.free_gift_log)
    f_df = f_df.rename(columns={
        'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '個数', 
        'point': 'ポイント', 'created_at': 'raw_time', 'user_id': 'ユーザーID'
    })
    combined_list.append(f_df[['raw_time', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']])

# 3. マージとソート
if combined_list:
    merged_df = pd.concat(combined_list, ignore_index=True)
    
    # 時間で降順ソート（新しい順）
    merged_df = merged_df.sort_values(by='raw_time', ascending=False)
    
    # 表示用に時間をJST文字列に変換
    merged_df['ギフト時間'] = pd.to_datetime(
        merged_df['raw_time'], unit='s'
    ).dt.tz_localize('UTC').dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
    
    # カラム順を整理
    display_cols = ['ギフト時間', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']
    
    st.dataframe(merged_df[display_cols], use_container_width=True, hide_index=True)
    
    # 4. CSVダウンロードボタン
    buffer = io.BytesIO()
    merged_df[display_cols].to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)
    st.download_button(
        label="スペシャル＆無償ギフトログをCSVでダウンロード",
        data=buffer,
        file_name=f"all_gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="download_all_gift_csv"
    )
else:
    st.info("表示できるギフトデータがありません。")

# --- ▲▲▲ スペシャル＆無償マージ版 ここまで ▲▲▲ ---

# --- ▼▼▼ スペシャル＆無償ギフトログ一覧表 （ユーザー単位で集計） [名前変更対策版] ▼▼▼ ---

st.markdown("---")

# データがどちらか一方でもあれば実行
if st.session_state.gift_log or st.session_state.free_gift_log:
    all_data_for_agg = []

    # 1. スペシャルギフトの集計用準備
    if st.session_state.gift_log:
        s_df_agg = pd.DataFrame(st.session_state.gift_log)
        s_df_agg['created_at_dt'] = pd.to_datetime(s_df_agg['created_at'], unit='s')
        
        if st.session_state.gift_list_map:
            gift_info_df = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
            gift_info_df.index = gift_info_df.index.astype(str)
            s_df_agg['gift_id'] = s_df_agg['gift_id'].astype(str)
            s_df_agg = s_df_agg.set_index('gift_id').join(gift_info_df, on='gift_id', lsuffix='_u', rsuffix='_g').reset_index()
            
            s_df_agg = s_df_agg.rename(columns={
                'name_u': 'ユーザー名', 'name_g': 'ギフト名', 'num': '個数', 
                'point': 'ポイント', 'user_id': 'ユーザーID'
            })
            all_data_for_agg.append(s_df_agg[['ユーザー名', 'ユーザーID', 'ギフト名', 'ポイント', '個数', 'created_at_dt']])

    # 2. 無償ギフトの集計用準備
    if st.session_state.free_gift_log:
        f_df_agg = pd.DataFrame(st.session_state.free_gift_log)
        f_df_agg['created_at_dt'] = pd.to_datetime(f_df_agg['created_at'], unit='s')
        f_df_agg = f_df_agg.rename(columns={
            'name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '個数', 
            'point': 'ポイント', 'user_id': 'ユーザーID'
        })
        all_data_for_agg.append(f_df_agg[['ユーザー名', 'ユーザーID', 'ギフト名', 'ポイント', '個数', 'created_at_dt']])

    if all_data_for_agg:
        combined_agg_df = pd.concat(all_data_for_agg, ignore_index=True)

        # 最新の名前を特定
        latest_all_names = combined_agg_df.sort_values('created_at_dt').groupby('ユーザーID')['ユーザー名'].last().to_dict()

        # 4. ユーザー・ギフトごとの集計
        grouped = (
            combined_agg_df.groupby(['ユーザーID', 'ギフト名', 'ポイント'], as_index=False)
                           .agg({'個数': 'sum'})
        )
        grouped['ユーザー名'] = grouped['ユーザーID'].map(latest_all_names)
        grouped['行ポイント'] = grouped['個数'] * grouped['ポイント']

        # 5. ユーザー単位の総ポイントを計算
        user_total = grouped.groupby('ユーザーID')['行ポイント'].sum().reset_index()
        user_total = user_total.rename(columns={'行ポイント': 'ユーザー総ポイント'})
        grouped = grouped.merge(user_total, on='ユーザーID', how='left')

        # 6. ソート
        grouped_sorted = grouped.sort_values(
            by=['ユーザー総ポイント', 'ユーザーID', 'ポイント'],
            ascending=[False, True, False]
        )

        # 7. 表示用データの整形（★見出し名を変更）
        display_rows = []
        prev_user_id = None
        for _, row in grouped_sorted.iterrows():
            curr_user_id = row['ユーザーID']
            is_new_user = (curr_user_id != prev_user_id)
            
            display_rows.append({
                'ユーザー名': row['ユーザー名'] if is_new_user else '',
                'ギフト名': row['ギフト名'],
                '個数（合計）': row['個数'],
                'ポイント': row['ポイント'],
                '総貢献Pt（※単純合計値）': int(row['ユーザー総ポイント']) if is_new_user else ''
            })
            prev_user_id = curr_user_id

        final_agg_df = pd.DataFrame(display_rows)

        # 8. UI表示
        st.markdown(
            """
            <h3 style="font-size:1.5em; margin-bottom:6px;">
                🎁🎈 スペシャル＆無償ギフトログ一覧表
                <span style="font-size:0.7em; opacity:0.8;">（ユーザー単位で集計）</span>
            </h3>
            """,
            unsafe_allow_html=True
        )
        st.dataframe(final_agg_df, use_container_width=True, hide_index=True)

else:
    st.info("集計できるギフトデータがありません。")

# --- ▲▲▲ スペシャル＆無償ギフト集計版 ここまで ▲▲▲ ---

st.markdown("---")

# ファンリスト一覧表
if st.session_state.fan_list:
    fan_df = pd.DataFrame(st.session_state.fan_list)
    
    rename_map = {'user_name': 'ユーザー名', 'level': 'レベル', 'point': 'ポイント', 'user_id': 'ユーザーID'}
    if 'rank' in fan_df.columns:
        rename_map['rank'] = '順位'
    
    fan_df = fan_df.rename(columns=rename_map)

    final_fan_cols = [col for col in fan_cols if col in fan_df.columns]
    
    column_config = {
        "順位": st.column_config.NumberColumn("順位", help="ファンランキングの順位", width="small"),
        "レベル": st.column_config.NumberColumn("レベル", help="ファンレベル", width="small"),
        "ユーザー名": st.column_config.TextColumn("ユーザー名", help="SHOWROOMのユーザー名", width="large"),
        "ポイント": st.column_config.NumberColumn("ポイント", help="獲得ポイント", format="%d", width="medium"),
        "ユーザーID": st.column_config.NumberColumn("ユーザーID", help="SHOWROOMのユーザーID", width="medium")
    }
    
    st.markdown("#### 🏆 ファンリスト一覧表")
    st.dataframe(
        fan_df[final_fan_cols], 
        use_container_width=True, 
        hide_index=True,
        column_config=column_config
    )
    
    buffer = io.BytesIO()
    fan_df[final_fan_cols].to_csv(buffer, index=False, encoding='utf-8-sig')
    buffer.seek(0)
    st.download_button(
        label="ファンリストをCSVでダウンロード",
        data=buffer,
        file_name=f"fan_list_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )
else:
    st.info("ダウンロードできるファンデータがありません。")