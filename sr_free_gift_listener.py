# sr_free_gift_listener.py
import json
import threading
import websocket
import time

class FreeGiftListener:
    def __init__(self, room_id, on_gift_callback):
        self.room_id = int(room_id)
        self.on_gift_callback = on_gift_callback
        self.ws = None

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return

        # ★ 無償ギフトイベント（ここが重要）
        if data.get("type") == "free_gift":
            self.on_gift_callback(data)

    def _on_open(self, ws):
        # 視聴開始通知（ログイン不要）
        ws.send(json.dumps({
            "command": "watch",
            "room_id": self.room_id
        }))

    def _run(self):
        self.ws = websocket.WebSocketApp(
            "wss://www.showroom-live.com/socket",
            on_message=self._on_message,
            on_open=self._on_open
        )
        self.ws.run_forever(ping_interval=30)

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
