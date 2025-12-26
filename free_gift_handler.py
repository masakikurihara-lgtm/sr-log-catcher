import websocket
import json
import threading
import requests
import queue
import time
import streamlit as st

# --- 通信スレッドとメイン画面の間でデータを安全に受け渡すためのキュー ---
# これにより、WebSocketスレッドがエラーで止まるのを防ぎます
gift_queue = queue.Queue()

class FreeGiftReceiver:
    def __init__(self, room_id, host, key):
        self.room_id = room_id
        self.host = host
        self.key = key
        self.ws = None
        self.thread = None
        self.is_running = False

    def on_message(self, ws, message):
        """WebSocketからメッセージを受信した時の処理"""
        if message.startswith("MSG"):
            try:
                parts = message.split("\t")
                if len(parts) < 3:
                    return
                
                # JSON部分を解析
                data = json.loads(parts[2])

                # ギフトメッセージ(t=2)のみを対象
                if data.get("t") == 2:
                    # 重要：スレッド内から st.session_state を触ると落ちるため、
                    # 安全な gift_queue にデータを放り込むだけに留めます
                    gift_queue.put(data)
            except Exception as e:
                # スレッド内のエラーは標準出力（Logs）に送る
                print(f"WebSocket Message Error: {e}")

    def on_error(self, ws, error):
        print(f"WebSocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket Closed")
        self.is_running = False

    def on_open(self, ws):
        """接続直後に購読命令(SUB)を送る（テストコードと同じ）"""
        ws.send(f"SUB\t{self.key}")

    def run(self):
        """WebSocketのメインループ"""
        ws_url = f"wss://{self.host}:443/"
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        self.ws.run_forever()

    def start(self):
        """バックグラウンドで受信を開始"""
        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self):
        """受信を停止"""
        if self.ws:
            self.ws.close()
            self.is_running = False

def get_streaming_server_info(room_id):
    """
    テストコードで成功した live_info API を使用して接続情報を取得する。
    """
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
    """
    APIから無償ギフト（星・種）のマスター情報を取得して
    st.session_state.free_gift_master に保存する。
    """
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
                        # 修正：free かつ point が 1 のものだけを「無償ギフト（星・種）」とみなす
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