import streamlit as st
import requests
import pandas as pd
import pytz
import datetime
import io
from streamlit_autorefresh import st_autorefresh
import ftplib
import os
import websocket
import threading
import json
import queue

# --- [追加] WebSocket監視用クラス ---
class ShowroomWSListener(threading.Thread):
    def __init__(self, room_id, gift_queue, gift_master):
        super().__init__(daemon=True)
        self.room_id = room_id
        self.gift_queue = gift_queue
        self.gift_master = gift_master
        self.ws = None

    def run(self):
        try:
            res = requests.get(f"https://www.showroom-live.com/api/live/streaming_url?room_id={self.room_id}")
            data = res.json()
            host = data.get("broadcast_host")
            key = data.get("broadcast_key") or data.get("key")
            if not host or not key: return

            ws_url = f"wss://{host}/"
            self.ws = websocket.WebSocketApp(
                ws_url,
                on_open=lambda ws: ws.send(f"SUB {key}\n"),
                on_message=self.on_message
            )
            self.ws.run_forever()
        except Exception as e:
            print(f"WS Thread Error: {e}")

    def on_message(self, ws, message):
        if message.startswith("MSG"):
            parts = message.split(" ", 3)
            if len(parts) >= 4 and parts[2] == "2":
                try:
                    payload = json.loads(parts[3])
                    # gt=1 が無償ギフト(星・種)
                    if payload.get("gt") == 1:
                        gift_id = payload.get("g")
                        gift_name = self.gift_master.get(gift_id, f"無償ギフト({gift_id})")
                        gift_data = {
                            "created_at": int(datetime.datetime.now().timestamp()),
                            "user_name": payload.get("ua"),
                            "gift_name": gift_name,
                            "num": int(payload.get("n", 1)),
                            "point": int(payload.get("n", 1)),
                            "user_id": payload.get("u"),
                            "image": f"https://static.showroom-live.com/image/gift/{gift_id}_s.png" # アイコンURL生成
                        }
                        self.gift_queue.put(gift_data)
                except: pass

    def stop(self):
        if self.ws: self.ws.close()

# --- [追加] ギフト名取得関数 ---
def get_gift_master(room_id):
    try:
        res = requests.get(f"https://www.showroom-live.com/api/live/gift_list?room_id={room_id}")
        data = res.json()
        master = {}
        for cat in data.get("gift_categories", []):
            for g in cat.get("gifts", []):
                master[g["gift_id"]] = g["gift_name"]
        return master
    except: return {}

# --- 既存のFTPアップロード関数 (そのまま維持) ---
def upload_csv_to_ftp(filename: str, csv_buffer: io.BytesIO):
    """Secretsに登録されたFTP設定を使ってCSVをアップロード"""
    if "ftp" not in st.secrets:
        st.error("FTP設定がsecrets.tomlにありません。")
        return
    ftp_info = st.secrets["ftp"]
    try:
        ftp = ftplib.FTP(ftp_info["host"])
        ftp.login(ftp_info["user"], ftp_info["password"])
        ftp.cwd("/rokudouji.net/mksoul/showroom_onlives_logs")

        csv_buffer.seek(0)
        ftp.storbinary(f"STOR {filename}", csv_buffer)

        file_list = []
        ftp.retrlines("LIST", file_list.append)
        now = datetime.datetime.now()
        for entry in file_list:
            parts = entry.split(maxsplit=8)
            if len(parts) < 9: continue
            name = parts[-1]
            if not name.endswith(".csv"): continue
            try:
                time_str = name.split("_")[-1].replace(".csv", "")
                file_dt = datetime.datetime.strptime(time_str, "%Y%m%d%H%M%S")
                if (now - file_dt).total_seconds() > 48 * 3600:
                    ftp.delete(name)
            except: continue
        ftp.quit()
    except Exception as e:
        st.error(f"FTPアップロードエラー: {e}")

def get_room_id_from_url_key(room_url_key):
    url = f"https://www.showroom-live.com/api/room/status?room_url_key={room_url_key}"
    res = requests.get(url)
    if res.status_code == 200:
        return res.json().get("room_id")
    return None

# --- 既存関数を拡張 (WebSocketデータ統合) ---
def get_and_update_log(room_id, last_comment_id, last_gift_created_at):
    new_comments = []
    new_gifts = []

    # 1. 有償ギフト (APIから取得)
    gift_url = f"https://www.showroom-live.com/api/live/gift_log?room_id={room_id}"
    res_gift = requests.get(gift_url)
    if res_gift.status_code == 200:
        all_gifts = res_gift.json().get("gift_log", [])
        for g in all_gifts:
            if g.get("created_at") > last_gift_created_at:
                new_gifts.append(g)

    # 2. [追加] 無償ギフト (WebSocketキューから取得)
    if "ws_gift_queue" in st.session_state:
        while not st.session_state.ws_gift_queue.empty():
            free_gift = st.session_state.ws_gift_queue.get()
            new_gifts.append(free_gift)

    # 3. コメント
    comment_url = f"https://www.showroom-live.com/api/live/comment_log?room_id={room_id}"
    res_comment = requests.get(comment_url)
    if res_comment.status_code == 200:
        all_comments = res_comment.json().get("comment_log", [])
        for c in all_comments:
            if int(c.get("comment_id", 0)) > int(last_comment_id):
                new_comments.append(c)

    return new_comments, new_gifts

def get_fan_list(room_id):
    url = f"https://www.showroom-live.com/api/live/summary_fan_ranking?room_id={room_id}"
    res = requests.get(url)
    if res.status_code == 200:
        return res.json().get("ranking", [])
    return []

# --- Streamlit UI メイン処理 ---
st.set_page_config(page_title="SHOWROOM ライブ配信ログ 収集ツール", layout="wide")

# (既存のCSS設定などはそのまま維持)
st.markdown("""
    <style>
    .main { background-color: #f0f2f6; }
    .stDataFrame { background-color: white; border-radius: 10px; padding: 10px; }
    </style>
""", unsafe_allow_html=True)

st.title("🎥 SHOWROOM ライブ配信ログ 収集ツール")

# セッション状態の初期化 (既存分 + WebSocket用)
if "is_running" not in st.session_state: st.session_state.is_running = False
if "comment_list" not in st.session_state: st.session_state.comment_list = []
if "gift_list" not in st.session_state: st.session_state.gift_list = []
if "fan_list" not in st.session_state: st.session_state.fan_list = []
if "last_comment_id" not in st.session_state: st.session_state.last_comment_id = 0
if "last_gift_created_at" not in st.session_state: st.session_state.last_gift_created_at = 0
if "ws_gift_queue" not in st.session_state: st.session_state.ws_gift_queue = queue.Queue()
if "ws_thread" not in st.session_state: st.session_state.ws_thread = None

with st.sidebar:
    st.header("⚙️ 設定")
    room_url_key = st.text_input("ルームURLキーを入力 (例: 46_HINATAZAKA46)", value="46_HINATAZAKA46")
    refresh_interval = st.slider("更新間隔 (秒)", 5, 60, 10)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔴 追跡開始", use_container_width=True, disabled=st.session_state.is_running):
            room_id = get_room_id_from_url_key(room_url_key)
            if room_id:
                st.session_state.room_id = room_id
                st.session_state.is_running = True
                st.session_state.comment_list = []
                st.session_state.gift_list = []
                st.session_state.last_comment_id = 0
                st.session_state.last_gift_created_at = int(datetime.datetime.now().timestamp())
                
                # [追加] 無償ギフト監視開始
                gift_master = get_gift_master(room_id)
                st.session_state.ws_thread = ShowroomWSListener(room_id, st.session_state.ws_gift_queue, gift_master)
                st.session_state.ws_thread.start()
                
                st.rerun()
            else:
                st.error("ルームIDが取得できませんでした。")

    with col2:
        if st.button("⏹️ 追跡停止", use_container_width=True, disabled=not st.session_state.is_running):
            if st.session_state.ws_thread:
                st.session_state.ws_thread.stop()
                st.session_state.ws_thread = None
            st.session_state.is_running = False
            st.rerun()

# 既存のメインループ処理 (そのまま維持)
if st.session_state.is_running:
    st_autorefresh(interval=refresh_interval * 1000, key="datarefresh")
    
    room_id = st.session_state.room_id
    new_comments, new_gifts = get_and_update_log(
        room_id, 
        st.session_state.last_comment_id, 
        st.session_state.last_gift_created_at
    )
    
    if new_comments:
        st.session_state.comment_list.extend(new_comments)
        st.session_state.last_comment_id = max([int(c.get("comment_id", 0)) for c in new_comments])
    
    if new_gifts:
        st.session_state.gift_list.extend(new_gifts)
        st.session_state.last_gift_created_at = max([g.get("created_at") for g in new_gifts])

    st.session_state.fan_list = get_fan_list(room_id)

# --- 表示セクション ---
jst = pytz.timezone('Asia/Tokyo')

# ギフト表示 (既存のカラム設定を維持しつつ無償ギフトも混ざる)
if st.session_state.gift_list:
    st.markdown("### 🎁 ギフトログ一覧表 (有償+無償)")
    df_gift = pd.DataFrame(st.session_state.gift_list)
    df_gift['時刻'] = pd.to_datetime(df_gift['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(jst).dt.strftime('%H:%M:%S')
    
    # 元のカラム設定を適用
    rename_map = {'user_name': 'ユーザー名', 'gift_name': 'ギフト名', 'num': '個数', 'point': 'ポイント', 'user_id': 'ユーザーID'}
    df_gift = df_gift.rename(columns=rename_map)
    
    gift_cols = ['時刻', 'ユーザー名', 'ギフト名', '個数', 'ポイント', 'ユーザーID']
    st.dataframe(df_gift[gift_cols], use_container_width=True, hide_index=True)

# コメント表示 (既存ロジックそのまま)
if st.session_state.comment_list:
    st.markdown("### 💬 コメントログ一覧表")
    df_comment = pd.DataFrame(st.session_state.comment_list)
    df_comment['時刻'] = pd.to_datetime(df_comment['created_at'], unit='s').dt.tz_localize('UTC').dt.tz_convert(jst).dt.strftime('%H:%M:%S')
    df_comment = df_comment.rename(columns={'user_name': 'ユーザー名', 'comment': 'コメント', 'user_id': 'ユーザーID'})
    st.dataframe(df_comment[['時刻', 'ユーザー名', 'コメント', 'ユーザーID']], use_container_width=True, hide_index=True)

# ファンランキング表示 (既存ロジックそのまま)
if st.session_state.fan_list:
    st.markdown("### 🏆 ファンリスト一覧表")
    fan_df = pd.DataFrame(st.session_state.fan_list).rename(columns={'user_name': 'ユーザー名', 'level': 'レベル', 'point': 'ポイント', 'user_id': 'ユーザーID'})
    st.dataframe(fan_df[['ユーザー名', 'レベル', 'ポイント', 'ユーザーID']], use_container_width=True, hide_index=True)

# CSVダウンロード＆FTPアップロード (既存ロジックそのまま)
if st.session_state.is_running or st.session_state.gift_list:
    if st.button("📥 データをCSV保存してFTPアップロード"):
        now_str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        if st.session_state.gift_list:
            df_g = pd.DataFrame(st.session_state.gift_list)
            csv_buf = io.BytesIO()
            df_g.to_csv(csv_buf, index=False, encoding='utf-8-sig')
            upload_csv_to_ftp(f"gift_log_{room_url_key}_{now_str}.csv", csv_buf)
            st.success("ギフトログをアップロードしました。")