import streamlit as st
import requests
import pandas as pd
import pytz
import datetime
from streamlit_autorefresh import st_autorefresh

# ページ設定
st.set_page_config(
    page_title="SHOWROOM ライバーサポートツール",
    page_icon="🎤",
    layout="wide",
)

# 定数
HEADERS = {"User-Agent": "Mozilla/5.0"}
JST = pytz.timezone('Asia/Tokyo')
LIVE_API_URL = "https://www.showroom-live.com/api/live/onlives"
COMMENT_API_URL = "https://www.showroom-live.com/api/live/comment_log"
GIFT_API_URL = "https://www.showroom-live.com/api/live/gift_log"
GIFT_LIST_API_URL = "https://www.showroom-live.com/api/live/gift_list"
FAN_LIST_API_URL = "https://www.showroom-live.com/api/active_fan/users"

# CSSスタイル
# - コンテナの固定高さとスクロールバー
# - コメント/ギフト/ファンリストのアイテム表示
# - ハイライトカラー（前回のツールから流用）
CSS_STYLE = """
<style>
.dashboard-container {
    height: 500px;
    overflow-y: scroll;
    padding-right: 15px;
}
.comment-item, .gift-item, .fan-item {
    border-bottom: 1px solid #eee;
    padding: 8px 0;
}
.comment-item:last-child, .gift-item:last-child, .fan-item:last-child {
    border-bottom: none;
}
.comment-time {
    font-size: 0.8em;
    color: #888;
}
.comment-user {
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
.fan-info-row {
    display: flex;
    align-items: center;
    gap: 10px;
}
.fan-level {
    font-weight: bold;
    color: #555;
}
</style>
"""
st.markdown(CSS_STYLE, unsafe_allow_html=True)

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

# --- API連携関数 ---

@st.cache_data(ttl=300)
def get_onlives_data():
    """全ライブ配信中のルーム情報を取得"""
    try:
        response = requests.get(LIVE_API_URL, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        all_lives = []
        if isinstance(data, dict):
            for live_type in ['onlives', 'official_lives', 'talent_lives', 'amateur_lives']:
                if live_type in data and isinstance(data.get(live_type), list):
                    all_lives.extend(data[live_type])
        return all_lives
    except requests.exceptions.RequestException as e:
        st.error(f"ライブ情報取得中にエラーが発生しました: {e}")
        return []
    except (ValueError, AttributeError):
        st.error("ライブ情報のJSONデコードまたは解析に失敗しました。")
        return []

def get_and_update_log(log_type, room_id):
    """コメントまたはギフトのログを取得・更新"""
    api_url = COMMENT_API_URL if log_type == "comment" else GIFT_API_URL
    url = f"{api_url}?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        new_log = response.json().get(f'{log_type}_log', [])
        
        # セッションキャッシュにないログだけ追加する
        existing_cache = st.session_state[f"{log_type}_log"]
        existing_log_keys = {
            (log.get('created_at'), log.get('name'), log.get('comment', log.get('gift_id')))
            for log in existing_cache
        }
        for log in new_log:
            log_key = (log.get('created_at'), log.get('name'), log.get('comment', log.get('gift_id')))
            if log_key not in existing_log_keys:
                existing_cache.append(log)
        
        # 新しい順にソート
        existing_cache.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        return existing_cache
    except requests.exceptions.RequestException as e:
        st.warning(f"ルームID {room_id} の{log_type}ログ取得中にエラーが発生しました。配信中か確認してください。")
        return st.session_state.get(f"{log_type}_log", [])

def get_gift_list(room_id):
    """ギフトリスト（ポイント情報）を取得・キャッシュする"""
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
    """ファンリストを取得"""
    current_ym = datetime.datetime.now(JST).strftime("%Y%m")
    url = f"{FAN_LIST_API_URL}?room_id={room_id}&ym={current_ym}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        data = response.json()
        return data.get("users", [])
    except requests.exceptions.RequestException as e:
        st.warning(f"ルームID {room_id} のファンリスト取得中にエラーが発生しました。")
        return []

# --- UI構築 ---

st.markdown("<h1 style='font-size:2.5em;'>🎤 ライバーサポートツール</h1>", unsafe_allow_html=True)
st.write("ライバーの配信中のコメント、ギフト、ファンリストをリアルタイムで追跡し、ログをダウンロードできます。")

input_room_id = st.text_input("対象のルームIDを入力してください:", placeholder="例: 444545", key="room_id_input")

col1, col2 = st.columns([1, 4])
with col1:
    if col1.button("トラッキング開始", key="start_button"):
        st.session_state.is_tracking = True
        st.session_state.room_id = input_room_id
        # データ初期化
        st.session_state.comment_log = []
        st.session_state.gift_log = []
        st.session_state.gift_list_map = {}
        st.session_state.fan_list = []
        st.rerun()

with col2:
    if col2.button("トラッキング停止", key="stop_button", disabled=not st.session_state.is_tracking):
        st.session_state.is_tracking = False
        st.session_state.room_info = None
        st.info("トラッキングを停止しました。")
        st.rerun()

if st.session_state.is_tracking:
    live_info = get_onlives_data()
    target_room = next((r for r in live_info if str(r.get('room_id')) == str(st.session_state.room_id)), None)

    if target_room:
        st.success(f"ルーム「{target_room.get('main_name')}」の配信をトラッキング中です！")
        
        # 7秒ごとに自動更新
        st_autorefresh(interval=7000, limit=None, key="dashboard_refresh")
        
        # 各ログを取得
        st.session_state.comment_log = get_and_update_log("comment", st.session_state.room_id)
        st.session_state.gift_log = get_and_update_log("gift", st.session_state.room_id)
        st.session_state.gift_list_map = get_gift_list(st.session_state.room_id)
        st.session_state.fan_list = get_fan_list(st.session_state.room_id)

        st.markdown("---")
        st.markdown("<h2 style='font-size:2em;'>📊 リアルタイム・ダッシュボード</h2>", unsafe_allow_html=True)
        st.markdown(f"**最終更新日時 (日本時間): {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}**")
        st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※約7秒ごとに自動更新されます。</p>", unsafe_allow_html=True)

        col_comment, col_gift, col_fan = st.columns(3)

        # コメントコンテナ
        with col_comment:
            st.markdown("### 📝 コメントログ")
            st.markdown("<div class='dashboard-container'>", unsafe_allow_html=True)
            if st.session_state.comment_log:
                for log in st.session_state.comment_log:
                    user_name = log.get('name', '匿名ユーザー')
                    comment_text = log.get('comment', '')
                    created_at = datetime.datetime.fromtimestamp(log.get('created_at', 0), JST).strftime("%H:%M:%S")
                    html = f"""
                    <div class="comment-item">
                        <div class="comment-time">{created_at}</div>
                        <div class="comment-user">{user_name}</div>
                        <div class="comment-text">{comment_text}</div>
                    </div>
                    """
                    st.markdown(html, unsafe_allow_html=True)
            else:
                st.info("コメントがありません。")
            st.markdown("</div>", unsafe_allow_html=True)

        # ギフトコンテナ
        with col_gift:
            st.markdown("### 🎁 ギフトログ")
            st.markdown("<div class='dashboard-container'>", unsafe_allow_html=True)
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
                    
                    html = f"""
                    <div class="gift-item {highlight_class}">
                        <div class="comment-time">{created_at}</div>
                        <div class="gift-info-row">
                            <img src="{gift_image_url}" class="gift-image" />
                            <span>×{gift_count}</span>
                        </div>
                        <div>{user_name} ({gift_point}pt)</div>
                    </div>
                    """
                    st.markdown(html, unsafe_allow_html=True)
            else:
                st.info("ギフトがありません。")
            st.markdown("</div>", unsafe_allow_html=True)

        # ファンリストコンテナ
        with col_fan:
            st.markdown("### 🏆 ファンリスト")
            st.markdown("<div class='dashboard-container'>", unsafe_allow_html=True)
            if st.session_state.fan_list:
                for fan in st.session_state.fan_list:
                    html = f"""
                    <div class="fan-item">
                        <div class="fan-info-row">
                            <img src="https://static.showroom-live.com/image/avatar/{fan.get('avatar_id', 0)}.png?v=108" width="30" height="30" style="border-radius:50%;" />
                            <div>
                                <div class="fan-level">Lv. {fan.get('level', 0)}</div>
                                <div>{fan.get('user_name', '不明なユーザー')}</div>
                            </div>
                        </div>
                    </div>
                    """
                    st.markdown(html, unsafe_allow_html=True)
            else:
                st.info("ファンデータがありません。")
            st.markdown("</div>", unsafe_allow_html=True)
        
        st.markdown("---")
        st.markdown("<h2 style='font-size:2em;'>📝 ログのダウンロード</h2>", unsafe_allow_html=True)
        st.markdown(f"<p style='font-size:12px; color:#a1a1a1;'>※データは現在{len(st.session_state.comment_log)}件のコメントと{len(st.session_state.gift_log)}件のギフトが蓄積されています。</p>", unsafe_allow_html=True)

        download_col1, download_col2 = st.columns(2)
        
        # コメントログをCSV化
        if st.session_state.comment_log:
            comment_df = pd.DataFrame(st.session_state.comment_log)
            comment_df['created_at'] = pd.to_datetime(comment_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST)
            comment_df = comment_df.rename(columns={
                'name': 'ユーザー名', 'comment': 'コメント内容', 'created_at': 'コメント時間', 'user_id': 'ユーザーID'
            })
            csv_comment = comment_df[['コメント時間', 'ユーザー名', 'ユーザーID', 'コメント内容']].to_csv(index=False, encoding='utf-8-sig')
            with download_col1:
                st.download_button(
                    label="コメントログをCSVでダウンロード",
                    data=csv_comment,
                    file_name=f"comment_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                )
        else:
            download_col1.info("ダウンロードできるコメントがありません。")
        
        # ギフトログをCSV化
        if st.session_state.gift_log:
            gift_df = pd.DataFrame(st.session_state.gift_log)
            gift_df['created_at'] = pd.to_datetime(gift_df['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(JST)
            
            # ギフト情報を結合
            if st.session_state.gift_list_map:
                gift_info_df = pd.DataFrame.from_dict(st.session_state.gift_list_map, orient='index')
                gift_info_df.index = gift_info_df.index.astype(int)
                gift_df = gift_df.set_index('gift_id').join(gift_info_df, on='gift_id').reset_index()

            gift_df = gift_df.rename(columns={
                'name_x': 'ユーザー名', 'name_y': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'created_at': 'ギフト時間', 'user_id': 'ユーザーID'
            })
            
            csv_gift = gift_df[['ギフト時間', 'ユーザー名', 'ユーザーID', 'ギフト名', '個数', 'ポイント']].to_csv(index=False, encoding='utf-8-sig')
            with download_col2:
                st.download_button(
                    label="ギフトログをCSVでダウンロード",
                    data=csv_gift,
                    file_name=f"gift_log_{st.session_state.room_id}_{datetime.datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                )
        else:
            download_col2.info("ダウンロードできるギフトがありません。")

    else:
        st.warning("指定されたルームIDが見つからないか、現在配信中ではありません。")
        st.session_state.is_tracking = False