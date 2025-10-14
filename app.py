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
.comment-content, .gift-content {
    flex-grow: 1;
    display: flex;
    flex-direction: column;
}
.comment-time, .gift-time {
    font-size: 0.8em;
    color: #888;
}
.comment-user, .gift-user {
    font-weight: bold;
    color: #333;
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
    margin-bottom: -36px !important;
    margin-top: 0 !important;
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

# --- UI構築 ---

st.markdown("<h1 style='font-size:2.5em;'>🎤 SHOWROOM 配信ログ収集ツール</h1>", unsafe_allow_html=True)
st.write("配信中のコメント、スペシャルギフト、ファンリストをリアルタイムで収集し、ログをダウンロードできます。")
st.write("")


# ▼▼ 認証ステップ ▼▼
if not st.session_state.authenticated:
    st.markdown("### 🔑 認証コードを入力してください")
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
            st.session_state.is_tracking = True
            st.session_state.room_id = input_room_id
            st.session_state.comment_log = []
            st.session_state.gift_log = []
            st.session_state.gift_list_map = {}
            st.session_state.fan_list = []
            st.session_state.total_fan_count = 0
            st.rerun()
    else:
        st.error("ルームIDを入力してください。")

if st.button("トラッキング停止", key="stop_button", disabled=not st.session_state.is_tracking):
    if st.session_state.is_tracking:
        # コメント・ギフト共に共通フォーマットで保存
        save_log_to_ftp("comment")
        save_log_to_ftp("gift")

    st.session_state.is_tracking = False
    st.session_state.room_info = None
    st.info("トラッキングを停止しました。")
    st.rerun()


if st.session_state.is_tracking:
    onlives_data = get_onlives_rooms()
    target_room_info = onlives_data.get(int(st.session_state.room_id)) if st.session_state.room_id.isdigit() else None

    # --- 配信終了検知と自動保存処理 ---
    is_live_now = int(st.session_state.room_id) in onlives_data

    if not is_live_now:
        st.warning("📡 配信が終了しました。ログを自動保存します。")

        # コメントログ保存
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

        # ギフトログ保存
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

        # 状態変更とリロード
        st.session_state.is_tracking = False
        st.info("✅ 配信終了を検知し、自動保存・トラッキング停止しました。")
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

        st_autorefresh(interval=7000, limit=None, key="dashboard_refresh")
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

        st.markdown("---")
        st.markdown("<h2 style='font-size:2em;'>📊 リアルタイムダッシュボード</h2>", unsafe_allow_html=True)
        st.markdown(f"**最終更新日時 (日本時間): {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}**")
        st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※約7秒ごとに自動更新されます。</p>", unsafe_allow_html=True)

        col_comment, col_gift, col_fan = st.columns(3)
        with col_comment:
            st.markdown("### 📝 コメント")
            with st.container(border=True, height=500):
                filtered_comments = [
                    log for log in st.session_state.comment_log 
                    if not any(keyword in log.get('name', '') or keyword in log.get('comment', '') for keyword in SYSTEM_COMMENT_KEYWORDS)
                ]
                if filtered_comments:
                    for log in filtered_comments:
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
                    st.info("コメントがありません。")
        with col_gift:
            st.markdown("### 🎁 スペシャルギフト")
            with st.container(border=True, height=500):
                if st.session_state.gift_log and st.session_state.gift_list_map:
                    for log in st.session_state.gift_log:
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
                    st.info("ギフトがありません。")
        with col_fan:
            st.markdown("### 🏆 ファンリスト")
            with st.container(border=True, height=500):
                if st.session_state.fan_list:
                    for fan in st.session_state.fan_list:
                        html = f"""
                        <div class="fan-item">
                            <div class="fan-info-row">
                                <img src="https://static.showroom-live.com/image/avatar/{fan.get('avatar_id', 0)}.png?v=108" class="fan-avatar" />
                                <div>
                                    <div class="fan-level">Lv. {fan.get('level', 0)}</div>
                                    <div>{fan.get('user_name', '不明なユーザー')}</div>
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
st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※データは現在{len(st.session_state.comment_log)}件のコメントと{len(st.session_state.gift_log)}件のスペシャルギフトと{st.session_state.total_fan_count}名のファンのデータが蓄積されています。</p>", unsafe_allow_html=True)
st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※誤ってリロード（再読み込み）してしまった、閉じてしまった等でダウンロードせずに消失してしまった場合、24時間以内に運営ご相談いただければ、復元・ログ取得できる可能性があります。</p>", unsafe_allow_html=True)

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
    st.markdown("### 📝 コメントログ一覧表")
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

# ギフト一覧表
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
    st.markdown("### 🎁 スペシャルギフトログ一覧表")
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
    
    st.markdown("### 🏆 ファンリスト一覧表")
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