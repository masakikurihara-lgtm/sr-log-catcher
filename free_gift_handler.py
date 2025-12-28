import websocket
import json
import threading
import requests
import queue
import time
import streamlit as st

# --- 修正の要：グローバルな gift_queue は使わず、セッションごとにキューを管理する ---
# 各ブラウザタブのスレッドを管理するリスト
active_receivers = []
receivers_lock = threading.Lock()

class FreeGiftReceiver:
    def __init__(self, room_id, host, key):
        self.room_id = room_id
        self.host = host
        self.key = key
        self.ws = None
        self.thread = None
        self.is_running = False
        # ★重要：このタブ専用のキューを作成
        self.my_queue = queue.Queue()

    def on_message(self, ws, message):
        if message.startswith("MSG"):
            try:
                parts = message.split("\t")
                if len(parts) < 3: return
                data = json.loads(parts[2])

                if data.get("t") == 2:
                    # 受信したデータを、自分専用の箱に入れる
                    self.my_queue.put(data)
            except Exception as e:
                print(f"WebSocket Message Error: {e}")

    def on_error(self, ws, error):
        print(f"WebSocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket Closed")

    def on_open(self, ws):
        ws.send(f"SUB\t{self.key}")
        print(f"WebSocket Connected: Room {self.room_id}")

    def run(self):
        ws_url = f"wss://{self.host}:443/"
        while self.is_running:
            try:
                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"WebSocket Run Error: {e}")
            
            if self.is_running:
                time.sleep(5)

    def start(self):
        if not self.is_running:
            self.is_running = True
            with receivers_lock:
                active_receivers.append(self)
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self):
        self.is_running = False
        if self.ws:
            self.ws.close()
        with receivers_lock:
            if self: active_receivers.remove(self)

# --- 本体側の「gift_queue」という名前に対応するためのダミーオブジェクト ---
# 本体側が「from free_gift_handler import gift_queue」していてもエラーにならないようにします
class QueueProxy:
    def empty(self):
        # 現在実行中のスレッド（セッション）に紐づくレシーバーのキューを確認
        receiver = st.session_state.get("ws_receiver")
        if receiver and hasattr(receiver, 'my_queue'):
            return receiver.my_queue.empty()
        return True

    def get_nowait(self):
        receiver = st.session_state.get("ws_receiver")
        if receiver and hasattr(receiver, 'my_queue'):
            return receiver.my_queue.get_nowait()
        raise queue.Empty

# 本体側が「gift_queue」としてインポートして使うための実体
gift_queue = QueueProxy()

# --- 以下の関数は元のロジックと一文字一句変えていません ---

def get_streaming_server_info(room_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    }
    try:
        url = f"https://www.showroom-live.com/api/live/live_info?room_id={room_id}"
        res = requests.get(url, headers=headers, timeout=5).json()
        host = res.get("bcsvr_host")
        key = res.get("bcsvr_key")
        if host and key:
            return {"host": host, "key": key}
    except Exception as e:
        print(f"API Error (live_info): {e}")
    return None

def update_free_gift_master(room_id):
    headers = {
        "User-Agent": "Mozilla/5.0",
    }
    try:
        url = f"https://www.showroom-live.com/api/live/gift_list?room_id={room_id}"
        res = requests.get(url, headers=headers, timeout=5).json()
        master = {}
        for cat_key in res.keys():
            category_list = res[cat_key]
            if isinstance(category_list, list):
                for category in category_list:
                    gifts = category.get("list", [])
                    for gift in gifts:
                        if gift.get("free") == True and gift.get("point") == 1:
                            master[str(gift["gift_id"])] = {
                                "name": gift["gift_name"],
                                "image": gift["image"],
                                "point": 1
                            }
        st.session_state.free_gift_master = master
    except Exception as e:
        print(f"API Error (gift_list): {e}")
        if "free_gift_master" not in st.session_state:
            st.session_state.free_gift_master = {}