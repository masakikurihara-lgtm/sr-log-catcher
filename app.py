import streamlit as st
import requests
import pandas as pd
import io
import time
import plotly.express as px
from streamlit_autorefresh import st_autorefresh
import logging
import re  # 追加：表示文字列から数値を抽出するため
import datetime
import pytz


# 日本時間で「今日の日付」を取得
JST = pytz.timezone("Asia/Tokyo")
today = datetime.datetime.now(JST).date()


# Set page configuration
st.set_page_config(
    page_title="SHOWROOM Event Dashboard",
    page_icon="🎤",
    layout="wide",
)

HEADERS = {"User-Agent": "Mozilla/5.0"}
JST = pytz.timezone('Asia/Tokyo')
ROOM_LIST_URL = "https://mksoul-pro.com/showroom/file/room_list.csv"  #認証用
BACKUP_INDEX_URL = "https://mksoul-pro.com/showroom/file/sr-event-archive-list-index.txt" # バックアップインデックスURL
# 固定ファイルURLを定義
BACKUP_FILE_URL = "https://mksoul-pro.com/showroom/file/sr-event-archive.csv"

if "authenticated" not in st.session_state:  #認証用
    st.session_state.authenticated = False  #認証用


# ▼▼▼ ここから修正・追加した関数群 ▼▼▼

def normalize_event_id(val):
    """
    event_idを統一された文字列形式に正規化します。
    (例: 123, 123.0, "123", "123.0" -> "123")
    """
    if val is None:
        return None
    try:
        # 数値や数値形式の文字列を float -> int -> str の順で変換
        return str(int(float(val)))
    except (ValueError, TypeError):
        # 変換に失敗した場合は、そのままの文字列として扱う
        return str(val).strip()

@st.cache_data(ttl=3600)
def get_api_events(status, pages=10):
    """
    APIから指定されたステータスのイベントを取得する汎用関数
    """
    api_events = []
    page = 1
    for _ in range(pages):
        url = f"https://www.showroom-live.com/api/event/search?status={status}&page={page}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=5)
            response.raise_for_status()
            data = response.json()

            page_events = []
            if isinstance(data, dict):
                if 'events' in data:
                    page_events = data['events']
                elif 'event_list' in data:
                    page_events = data['event_list']
            elif isinstance(data, list):
                page_events = data

            if not page_events:
                break

            filtered_page_events = [
                event for event in page_events 
                if event.get("show_ranking") is not False or event.get("type_name") == "ランキング"
            ]
            api_events.extend(filtered_page_events)
            page += 1
        except requests.exceptions.RequestException as e:
            st.error(f"イベントデータ取得中にエラーが発生しました (status={status}): {e}")
            break
        except ValueError:
            st.error(f"APIからのJSONデコードに失敗しました: {response.text}")
            break
    return api_events


@st.cache_data(ttl=3600)
def get_backup_events(start_date, end_date):
    """
    固定バックアップファイルから指定された期間の終了イベントを取得する関数
    - API側のフィルタ (show_ranking is not False OR type_name == 'ランキング') を適用
    - event_name の接頭辞を「＜終了(BU)＞ 」に変更
    - 終了日 (ended_at) が新しいものほど上に並べて返す（降順）
    """
    columns = [
        'event_id', 'is_event_block', 'is_entry_scope_inner', 'event_name',
        'image_m', 'started_at', 'ended_at', 'event_url_key', 'show_ranking'
    ]

    try:
        response = requests.get(BACKUP_FILE_URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
        csv_data = response.content.decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(csv_data), dtype=str)
    except Exception as e:
        st.error(f"バックアップファイルの取得に失敗しました: {e}")
        return []

    # --- 列の補完（不足カラムがあれば追加） ---
    # API側のフィルタで type_name も参照する可能性があるため補完しておく
    expected_extra = ['type_name']
    for col in columns + expected_extra:
        if col not in df.columns:
            df[col] = None

    # 必要列のみ取り出す（type_name は最後に付ける）
    use_cols = columns + expected_extra
    df = df[use_cols]

    # 数値変換
    df['started_at'] = pd.to_numeric(df['started_at'], errors='coerce').fillna(0)
    df['ended_at'] = pd.to_numeric(df['ended_at'], errors='coerce').fillna(0)

    # 重複除去（event_id ベース。上書き方針は keep='first' を維持）
    df.drop_duplicates(subset=['event_id'], keep='first', inplace=True)

    # 日付範囲フィルタ（JST）
    start_datetime = JST.localize(datetime.datetime.combine(start_date, datetime.time.min))
    end_datetime = JST.localize(datetime.datetime.combine(end_date, datetime.time.max))
    df['ended_at_dt'] = pd.to_datetime(df['ended_at'], unit='s', utc=True).dt.tz_convert(JST)
    df = df[(df['ended_at_dt'] >= start_datetime) & (df['ended_at_dt'] <= end_datetime)]

    # --- show_ranking を適切にパース（文字列 'False' 等に対応） ---
    def _parse_show_ranking(v):
        if pd.isna(v):
            return None
        s = str(v).strip().lower()
        if s in ('false', '0', 'no', 'n', 'none', 'nan', ''):
            return False
        if s in ('true', '1', 'yes', 'y'):
            return True
        try:
            fv = float(s)
            return bool(int(fv))
        except Exception:
            return None

    df['show_ranking'] = df['show_ranking'].apply(_parse_show_ranking)

    # レコード化
    records = df.to_dict(orient='records')

    # --- API と同じフィルタを適用 ---
    # API側の条件: event.get("show_ranking") is not False OR event.get("type_name") == "ランキング"
    filtered = []
    for r in records:
        sr_val = r.get('show_ranking')          # bool or None
        tname = (r.get('type_name') or '').strip()
        if (sr_val is not False) or (tname == "ランキング"):
            filtered.append(r)

    # --- event_name の接頭辞を「＜終了(BU)＞」に整形（重複付与避ける） ---
    for r in filtered:
        name = str(r.get('event_name', '') or '')
        # 既に付いている可能性のある接頭辞を削除してから付ける
        name = name.replace('＜終了(BU)＞ ', '').replace('＜終了＞ ', '').strip()
        r['event_name'] = f"＜終了(BU)＞ {name}"

    # --- 終了日が新しいもの順（降順）にソートして返す ---
    filtered.sort(key=lambda x: x.get('ended_at', 0), reverse=True)

    return filtered



@st.cache_data(ttl=600)
def get_ongoing_events():
    """
    開催中のイベントを取得する
    """
    events = get_api_events(status=1)
    now_ts = datetime.datetime.now(JST).timestamp()

    # 念のため、本当に開催中のものだけをフィルタリング
    ongoing_events = [e for e in events if e.get('ended_at', 0) > now_ts]

    for event in ongoing_events:
        try:
            event['started_at'] = int(float(event.get('started_at', 0)))
            event['ended_at'] = int(float(event.get('ended_at', 0)))
        except (ValueError, TypeError):
            event['started_at'] = 0
            event['ended_at'] = 0
    return ongoing_events


@st.cache_data(ttl=3600)
def get_finished_events(start_date, end_date):
    """
    終了したイベントをAPIから取得して返す
    （終了1ヶ月以内が対象）
    """
    api_events_raw = get_api_events(status=4)
    now_ts = datetime.datetime.now(JST).timestamp()
    start_ts = JST.localize(datetime.datetime.combine(start_date, datetime.time.min)).timestamp()
    end_ts = JST.localize(datetime.datetime.combine(end_date, datetime.time.max)).timestamp()

    api_events = []
    for event in api_events_raw:
        ended_at = event.get('ended_at', 0)
        if not (start_ts <= ended_at <= end_ts and ended_at < now_ts):
            continue
        try:
            event['started_at'] = int(float(event.get('started_at', 0)))
            event['ended_at'] = int(float(ended_at))
            api_events.append(event)
        except (ValueError, TypeError):
            continue

    # 新しいものが上に来るようにソート
    api_events.sort(key=lambda x: x.get('ended_at', 0), reverse=True)

    for e in api_events:
        e['event_name'] = f"＜終了＞ {str(e.get('event_name', '')).replace('＜終了＞ ', '').strip()}"

    return api_events


# ▲▲▲ ここまで修正・追加した関数群 ▲▲▲


# --- 以下、既存の関数は変更なし（一部上書き・改良あり） ---

# ※ 取得API候補の順序を、要望どおり room_list -> ranking の順に変更
RANKING_API_CANDIDATES = [
    "https://www.showroom-live.com/api/event/room_list?event_id={event_id}&page={page}",
    "https://www.showroom-live.com/api/event/{event_url_key}/ranking?page={page}",
]

@st.cache_data(ttl=300)
def get_event_ranking_with_room_id(event_url_key, event_id, max_pages=10):
    """
    終了後の最終順位取得用関数を堅牢化
    - まず room_list?event_id=xxxxx を試し、そこから取れなければ /{event_url_key}/ranking を試す
    - 各APIレスポンスの構造差（'list', 'ranking', 'event_list' など）に対応
    戻り値: { room_name: { 'room_id': ..., 'rank': ..., 'point': ... }, ... } または None
    """
    all_ranking_data = []
    for base_url in RANKING_API_CANDIDATES:
        try:
            temp_ranking_data = []
            for page in range(1, max_pages + 1):
                url = base_url.format(event_url_key=event_url_key, event_id=event_id, page=page)
                response = requests.get(url, headers=HEADERS, timeout=10)
                if response.status_code == 404:
                    break
                response.raise_for_status()
                data = response.json()

                ranking_list = None
                # room_list の場合は 'list'
                if isinstance(data, dict):
                    if 'list' in data and isinstance(data['list'], list):
                        ranking_list = data['list']
                    elif 'ranking' in data and isinstance(data['ranking'], list):
                        ranking_list = data['ranking']
                    elif 'event_list' in data and isinstance(data['event_list'], list):
                        ranking_list = data['event_list']
                    elif 'data' in data and isinstance(data['data'], list):
                        ranking_list = data['data']
                elif isinstance(data, list):
                    ranking_list = data

                if not ranking_list:
                    # ページが空なら次のページは無いとみなしてループを抜ける
                    break
                temp_ranking_data.extend(ranking_list)
            # 取得したデータに room_id を含むものがあれば成功とみなす
            if temp_ranking_data and any(isinstance(r, dict) and ('room_id' in r or 'id' in r) for r in temp_ranking_data):
                all_ranking_data = temp_ranking_data
                break
        except requests.exceptions.RequestException:
            # この候補は失敗。次の候補へ
            continue
    if not all_ranking_data:
        return None

    room_map = {}
    for room_info in all_ranking_data:
        if not isinstance(room_info, dict):
            continue
        # room_id 抽出（文字列にして保存）
        room_id = room_info.get('room_id') or room_info.get('id')
        if room_id is None and 'room' in room_info and isinstance(room_info['room'], dict):
            room_id = room_info['room'].get('room_id') or room_info['room'].get('id')

        if room_id is None:
            # どうしてもroom_idがない場合スキップ
            continue
        room_id_str = str(room_id)

        # ルーム名の抽出（キー名はAPIでブレるため複数候補をチェック）
        room_name = (
            room_info.get('room_name') or room_info.get('name') or room_info.get('performer_name') 
            or room_info.get('user_name') or room_info.get('room_title')
        )
        if not room_name:
            # room_info のネストに存在する可能性をチェック
            if 'room' in room_info and isinstance(room_info['room'], dict):
                room_name = room_info['room'].get('room_name') or room_info['room'].get('name')
        if not room_name:
            # 名前が取れなければIDをキー名にして格納する（呼び出し側が探せるように）
            room_name = f"room_{room_id_str}"

        # ポイントの抽出（いくつかの候補フィールド）
        point = None
        for k in ('point', 'event_point', 'popularity_point', 'total_point'):
            if k in room_info and room_info.get(k) is not None:
                try:
                    point = int(float(room_info.get(k)))
                except Exception:
                    try:
                        point = int(re.sub(r'[^\d\-]', '', str(room_info.get(k)) or '0') or 0)
                    except:
                        point = 0
                break
        # event_entry 内に入っている場合
        if point is None and 'event_entry' in room_info and isinstance(room_info['event_entry'], dict):
            evp = room_info['event_entry'].get('event_point') or room_info['event_entry'].get('point')
            try:
                point = int(float(evp)) if evp is not None else 0
            except:
                point = 0
        if point is None:
            # fallback: 0
            point = 0

        # 順位の抽出
        rank = None
        for k in ('rank', 'position'):
            if k in room_info and room_info.get(k) is not None:
                try:
                    rank = int(float(room_info.get(k)))
                except:
                    try:
                        rank = int(re.sub(r'[^\d\-]', '', str(room_info.get(k)) or '0') or 0)
                    except:
                        rank = None
                break
        # event_entry に rank 情報がある場合
        if rank is None and 'event_entry' in room_info and isinstance(room_info['event_entry'], dict):
            rnk = room_info['event_entry'].get('rank')
            try:
                rank = int(float(rnk)) if rnk is not None else None
            except:
                rank = None

        # 最終的に room_map に登録
        room_map[str(room_name)] = {
            'room_id': room_id_str,
            'rank': rank,
            'point': point
        }

    return room_map

@st.cache_data(ttl=300)
def get_event_participant_count(event_url_key, event_id, max_pages=30):
    """
    イベント参加ルーム数を取得する（優先順）
    1) room_list?event_id=... の total_entries を優先
    2) list があれば len(list)
    3) フォールバックで ranking API をページめくりして合計件数を算出
    戻り値: int (参加ルーム数) または None (取得失敗)
    """
    # 1) room_list に問い合わせて total_entries を見る
    try:
        url_room_list = f"https://www.showroom-live.com/api/event/room_list?event_id={event_id}"
        resp = requests.get(url_room_list, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                # server が用意した total_entries があればそれを優先
                te = data.get("total_entries")
                if te is not None:
                    try:
                        return int(te)
                    except:
                        pass
                # なければ list の長さを返す（1ページ分）
                if isinstance(data.get("list"), list):
                    return len(data.get("list"))
    except requests.exceptions.RequestException:
        # room_list が使えない場合はフォールバックへ
        pass

    # 2) フォールバック: ranking API をページめくりして合計を算出
    total_count = 0
    try:
        base_url_candidates = [
            f"https://www.showroom-live.com/api/event/{event_url_key}/ranking?page={{page}}",
            f"https://www.showroom-live.com/api/event/ranking?event_id={event_id}&page={{page}}"
        ]
        for base_url in base_url_candidates:
            total_count = 0
            for page in range(1, max_pages + 1):
                url = base_url.format(page=page)
                r = requests.get(url, headers=HEADERS, timeout=8)
                if r.status_code == 404:
                    break
                r.raise_for_status()
                d = r.json()
                # ranking や event_list など候補を探す
                if isinstance(d, dict):
                    arr = d.get("ranking") or d.get("event_list") or d.get("list") or d.get("data")
                elif isinstance(d, list):
                    arr = d
                else:
                    arr = None

                if not arr:
                    break
                total_count += len(arr)
            if total_count > 0:
                return int(total_count)
    except requests.exceptions.RequestException:
        pass

    return None

def get_room_event_info(room_id):
    url = f"https://www.showroom-live.com/api/room/event_and_support?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        st.error(f"ルームID {room_id} のデータ取得中にエラーが発生しました: {e}")
        return None

@st.cache_data(ttl=60)
def get_block_event_overall_ranking(event_url_key, event_id=None, max_pages=30):
    """
    ブロックイベント全体のランキング（順位情報のみ）を取得する。
    /ranking?page=n で取得し、rank=0 のルームは room_list?event_id={event_id} で補完。
    """
    rank_map = {}
    ranking_url_template = f"https://www.showroom-live.com/api/event/{event_url_key}/ranking?page={{page}}"

    try:
        # --- まず通常の /ranking?page=n から取得 ---
        for page in range(1, max_pages + 1):
            url = ranking_url_template.format(page=page)
            response = requests.get(url, headers=HEADERS, timeout=10)
            if response.status_code == 404:
                break
            response.raise_for_status()
            data = response.json()
            ranking_list = data.get("ranking") or data.get("list") or data.get("event_list") or data.get("data") or []
            if not ranking_list:
                break

            for room_info in ranking_list:
                if not isinstance(room_info, dict):
                    continue
                rid = room_info.get("room_id") or room_info.get("id")
                rnk = room_info.get("rank") or room_info.get("position")
                if rid is None:
                    continue
                try:
                    rank_map[str(rid)] = int(float(rnk)) if rnk is not None else 0
                except Exception:
                    rank_map[str(rid)] = 0

        # --- rank=0 のルームを room_list から補完 ---
        if event_id and any(v == 0 for v in rank_map.values()):
            try:
                roomlist_url = f"https://www.showroom-live.com/api/event/room_list?event_id={event_id}"
                resp = requests.get(roomlist_url, headers=HEADERS, timeout=10)
                if resp.status_code == 200:
                    data2 = resp.json()
                    room_list = data2.get("list", [])
                    for info in room_list:
                        rid = info.get("room_id")
                        rnk = info.get("rank")
                        if not rid or rnk is None:
                            continue
                        rid_str = str(rid)
                        # ranking で 0 だったルームのみ補完
                        if rid_str in rank_map and rank_map[rid_str] == 0:
                            try:
                                rank_map[rid_str] = int(float(rnk))
                            except Exception:
                                pass
                        elif rid_str not in rank_map:
                            # /ranking で取得できなかったルームも追加
                            try:
                                rank_map[rid_str] = int(float(rnk))
                            except Exception:
                                continue
            except requests.exceptions.RequestException:
                pass

    except requests.exceptions.RequestException as e:
        st.warning(f"ブロックイベントの全体ランキング取得中にエラーが発生しました: {e}")

    return rank_map


@st.cache_data(ttl=30)
def get_gift_list(room_id):
    url = f"https://www.showroom-live.com/api/live/gift_list?room_id={room_id}"
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
        return gift_list_map
    except requests.exceptions.RequestException as e:
        st.error(f"ルームID {room_id} のギフトリスト取得中にエラーが発生しました: {e}")
        return {}

if "gift_log_cache" not in st.session_state:
    st.session_state.gift_log_cache = {}

def get_and_update_gift_log(room_id):
    url = f"https://www.showroom-live.com/api/live/gift_log?room_id={room_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=5)
        response.raise_for_status()
        new_gift_log = response.json().get('gift_log', [])

        if room_id not in st.session_state.gift_log_cache:
            st.session_state.gift_log_cache[room_id] = []

        existing_log = st.session_state.gift_log_cache[room_id]

        if new_gift_log:
            existing_log_set = {(log.get('gift_id'), log.get('created_at'), log.get('num')) for log in existing_log}

            for log in new_gift_log:
                log_key = (log.get('gift_id'), log.get('created_at'), log.get('num'))
                if log_key not in existing_log_set:
                    existing_log.append(log)

        st.session_state.gift_log_cache[room_id].sort(key=lambda x: x.get('created_at', 0), reverse=True)

        return st.session_state.gift_log_cache[room_id]

    except requests.exceptions.RequestException as e:
        st.warning(f"ルームID {room_id} のギフトログ取得中にエラーが発生しました。配信中か確認してください: {e}")
        return st.session_state.gift_log_cache.get(room_id, [])

def get_onlives_rooms():
    onlives = {}
    try:
        url = "https://www.showroom-live.com/api/live/onlives"
        response = requests.get(url, headers=HEADERS, timeout=5)
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
            started_at = None
            premium_room_type = 0
            if isinstance(room, dict):
                room_id = room.get('room_id')
                started_at = room.get('started_at')
                premium_room_type = room.get('premium_room_type', 0)
                if room_id is None and 'live_info' in room and isinstance(room['live_info'], dict):
                    room_id = room['live_info'].get('room_id')
                    started_at = room['live_info'].get('started_at')
                    premium_room_type = room['live_info'].get('premium_room_type', 0)
                if room_id is None and 'room' in room and isinstance(room['room'], dict):
                    room_id = room['room'].get('room_id')
                    started_at = room['room'].get('started_at')
                    premium_room_type = room['room'].get('premium_room_type', 0)
            if room_id and started_at is not None:
                try:
                    onlives[int(room_id)] = {'started_at': started_at, 'premium_room_type': premium_room_type}
                except (ValueError, TypeError):
                    continue
    except requests.exceptions.RequestException as e:
        st.warning(f"配信情報取得中にエラーが発生しました: {e}")
    except (ValueError, AttributeError):
        st.warning("配信情報のJSONデコードまたは解析に失敗しました。")
    return onlives

def get_rank_color(rank):
    """
    ランキングに応じたカラーコードを返す
    Plotlyのデフォルトカラーを参考に設定
    """
    colors = px.colors.qualitative.Plotly
    if rank is None:
        return "#A9A9A9"  # DarkGray
    try:
        rank_int = int(rank)
        if rank_int <= 0:
            return colors[0]
        return colors[(rank_int - 1) % len(colors)]
    except (ValueError, TypeError):
        return "#A9A9A9"

# ヘルパー：表示文字列から数値を抽出（"1,234（※集計中）" -> 1234）
def extract_int_from_mixed(val):
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except:
        pass
    s = str(val)
    # 数字とマイナスだけ残す
    digits = re.sub(r"[^\d\-]", "", s)
    if digits == "":
        return None
    try:
        return int(digits)
    except:
        try:
            return int(float(digits))
        except:
            return None

def main():
    st.markdown("<h1 style='font-size:2.5em;'>🎤 SHOWROOM Event Dashboard</h1>", unsafe_allow_html=True)
    st.write("イベント順位やポイント、ポイント差、スペシャルギフトの履歴、必要ギフト数などが、リアルタイムで可視化できるツールです。")


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

                    if input_room_id.strip() in valid_codes:
                        st.session_state.authenticated = True
                        st.success("✅ 認証に成功しました。ツールを利用できます。")
                        st.rerun()  # 認証成功後に再読み込み
                    else:
                        st.error("❌ 認証コードが無効です。正しい認証コードを入力してください。")
                except Exception as e:
                    st.error(f"認証リストを取得できませんでした: {e}")
            else:
                st.warning("認証コードを入力してください。")

        # 認証が終わるまで他のUIを描画しない
        st.stop()
    # ▲▲ 認証ステップここまで ▲▲


    if "room_map_data" not in st.session_state:
        st.session_state.room_map_data = None
    if "selected_event_name" not in st.session_state:
        st.session_state.selected_event_name = None
    if "selected_room_names" not in st.session_state:
        st.session_state.selected_room_names = []
    if "multiselect_default_value" not in st.session_state:
        st.session_state.multiselect_default_value = []
    if "multiselect_key_counter" not in st.session_state:
        st.session_state.multiselect_key_counter = 0
    if "show_dashboard" not in st.session_state:
        st.session_state.show_dashboard = False
    if "auto_refresh_enabled" not in st.session_state:
        st.session_state.auto_refresh_enabled = True  # 自動更新デフォルト：有効
    # ▼ 対象/敵ルームの前回値を保存する変数を初期化
    #if "prev_battle_target_room" not in st.session_state:
    #    st.session_state.prev_battle_target_room = None
    #if "prev_battle_enemy_room" not in st.session_state:
    #    st.session_state.prev_battle_enemy_room = None        

    st.markdown("<h2 style='font-size:2em;'>1. イベントを選択</h2>", unsafe_allow_html=True)



    # --- ▼▼▼ 修正版: イベント取得フロー（重複除外＋カレンダー初期値） ▼▼▼ ---
    event_status = st.radio(
        "イベントステータスを選択してください:",
        ("開催中", "終了", "終了(BU)"),
        horizontal=True,
        key="event_status_selector"
    )

    st.markdown(
        "<p style='font-size:12px; margin: -10px 0px 20px 0px; color:#a1a1a1;'>※イベント終了直後、キャッシュの関係で、一時的に「開催中」と「終了」のいずれにも重複してプルダウン選択肢として表示される場合があります。</p>",
        unsafe_allow_html=True
    )

    events = []
    if event_status == "開催中":
        with st.spinner('開催中のイベントを取得中...'):
            events = get_ongoing_events()
            # 開催中イベントは終了日時が近い順（昇順）
            events.sort(key=lambda x: x.get('ended_at', float('inf')))

    else:
        # ✅ JST基準の today をもとに30日幅を算出
        if event_status == "終了(BU)":
            # 「終了(BU)」は通常より1か月前の30日間（＝59日前〜30日前）
            default_start = today - datetime.timedelta(days=60)
            default_end = today - datetime.timedelta(days=30)
        else:
            # 「終了」は直近30日（＝29日前〜今日まで）
            default_start = today - datetime.timedelta(days=30)
            default_end = today

        # key を event_status ごとにユニークにして、既存 session_state による固定化を防ぐ
        date_input_key = f"date_range_selector_{event_status}"

        selected_date_range = st.date_input(
            "イベント**終了日**（期間）をカレンダーで選択してください:",
            (default_start, default_end),
            min_value=datetime.date(2020, 1, 1),
            max_value=today,
            key=date_input_key
        )

        if len(selected_date_range) == 2:
            start_date, end_date = selected_date_range
            if start_date > end_date:
                st.error("エラー: 開始日は終了日以前を選択してください。")
                st.stop()
            else:
                if event_status == "終了":
                    with st.spinner(f'終了イベント ({start_date}〜{end_date}) を取得中...'):
                        events = get_finished_events(start_date, end_date)

                elif event_status == "終了(BU)":
                    with st.spinner(f'バックアップイベント ({start_date}〜{end_date}) を取得中...'):
                        events = get_backup_events(start_date, end_date)
                        # 「終了(BU)」は終了日が新しいもの順（降順）
                        events.sort(key=lambda x: x.get("ended_at", 0), reverse=True)

                        # ----- 重複除外 -----
                        try:
                            ended_events = get_finished_events(start_date, end_date)
                            ended_ids = {
                                normalize_event_id(e.get("event_id"))
                                for e in ended_events
                                if e.get("event_id") is not None
                            }
                            filtered_events = []
                            for e in events:
                                eid_norm = normalize_event_id(e.get("event_id"))
                                if eid_norm is None:
                                    filtered_events.append(e)
                                elif eid_norm not in ended_ids:
                                    filtered_events.append(e)
                            events = filtered_events
                        except Exception as ex:
                            st.warning(f"バックアップイベントの重複除外処理でエラーが発生しました: {ex}")
        else:
            st.warning("有効な終了日（期間）を選択してください。")
            st.stop()
    # --- ▲▲▲ 修正版ここまで ▲▲▲ ---



    if not events:
        st.warning("表示可能なイベントが見つかりませんでした。")
        return


    event_options = {event['event_name']: event for event in events}
    selected_event_name = st.selectbox(
        "イベント名を選択してください:", 
        options=list(event_options.keys()), key="event_selector")

    st.markdown(
        "<p style='font-size:12px; margin: -10px 0px 20px 0px; color:#a1a1a1;'>※ランキング型イベントが対象になります。ただし、ブロック型イベントはポイントのみで順位表示（総合ランキング表示）しています（ブロック分けされた表示とはなっていません）。<!--<br />※終了済みイベントのポイント表示は、イベント終了日の翌日12:00頃までは「集計中」となり、その後ポイントが表示され、24時間経過するとクリアされます（0表示になります）。<br />※終了済みイベントは、イベント終了日の約1ヶ月後を目処にイベント一覧の選択対象から削除されます。--></p>",
        unsafe_allow_html=True
    )

    if not selected_event_name:
        st.warning("イベントを選択してください。")
        return

    selected_event_data = event_options.get(selected_event_name)
    event_url = f"https://www.showroom-live.com/event/{selected_event_data.get('event_url_key')}"
    started_at_dt = datetime.datetime.fromtimestamp(selected_event_data.get('started_at', 0), JST)
    ended_at_dt = datetime.datetime.fromtimestamp(selected_event_data.get('ended_at', 0), JST)
    event_period_str = f"{started_at_dt.strftime('%Y/%m/%d %H:%M')} - {ended_at_dt.strftime('%Y/%m/%d %H:%M')}"
    st.info(f"選択されたイベント: **{selected_event_name}**")

    st.markdown("<h2 style='font-size:2em;'>2. 比較したいルームを選択</h2>", unsafe_allow_html=True)
    selected_event_key = selected_event_data.get('event_url_key', '')
    selected_event_id = selected_event_data.get('event_id')

    # イベントを変更した場合、「上位10ルームまでを選択」のチェックボックスも初期化する
    if st.session_state.selected_event_name != selected_event_name or st.session_state.room_map_data is None:
        with st.spinner('イベント参加者情報を取得中...'):
            st.session_state.room_map_data = get_event_ranking_with_room_id(selected_event_key, selected_event_id)
        st.session_state.selected_event_name = selected_event_name
        st.session_state.selected_room_names = []
        st.session_state.multiselect_default_value = []
        st.session_state.multiselect_key_counter += 1
        # チェックボックスのキーが存在すればFalseに設定
        if 'select_top_10_checkbox' in st.session_state:
            st.session_state.select_top_10_checkbox = False
        st.session_state.show_dashboard = False
        st.rerun()

    room_count_text = ""
    if st.session_state.room_map_data:
        # total_entries を優先取得（room_list -> ranking のフォールバック）
        try:
            participant_count = get_event_participant_count(selected_event_key, selected_event_id, max_pages=30)
        except Exception:
            participant_count = None

        if participant_count is not None:
            room_count_text = f" （現在{int(participant_count)}ルーム参加）"
        else:
            # フォールバック: 既に取得した room_map の件数
            try:
                room_count = len(st.session_state.room_map_data)
                room_count_text = f" （現在{room_count}ルーム参加）"
            except Exception:
                room_count_text = ""
    st.markdown(f"**▶ [イベントページへ移動する]({event_url})**{room_count_text}", unsafe_allow_html=True)

    if not st.session_state.room_map_data:
        st.warning("このイベントの参加者情報を取得できませんでした。")
        return

    with st.form("room_selection_form"):
        select_top_10 = st.checkbox(
            "上位10ルームまでを選択（**※チェックされている場合はこちらが優先されます**）", 
            key="select_top_10_checkbox")
        room_map = st.session_state.room_map_data
        sorted_rooms = sorted(room_map.items(), key=lambda item: item[1].get('point', 0), reverse=True)
        room_options = [room[0] for room in sorted_rooms]
        top_10_rooms = room_options[:10]
        selected_room_names_temp = st.multiselect(
            "比較したいルームを選択 (複数選択可):", options=room_options,
            default=st.session_state.multiselect_default_value,
            key=f"multiselect_{st.session_state.multiselect_key_counter}")
        st.markdown("<p style='font-size:12px; margin: -10px 0px 20px 0px; color:#a1a1a1;'>※上位30ルームまで表示されます。下位ルームは非表示となります。</p>", unsafe_allow_html=True)
        submit_button = st.form_submit_button("表示する")

    if submit_button:
        #st.session_state.auto_refresh_enabled = True
        if st.session_state.select_top_10_checkbox:
            st.session_state.selected_room_names = top_10_rooms
            st.session_state.multiselect_default_value = top_10_rooms
            st.session_state.multiselect_key_counter += 1
        else:
            st.session_state.selected_room_names = selected_room_names_temp
            st.session_state.multiselect_default_value = selected_room_names_temp
        st.session_state.show_dashboard = True
        st.rerun()

    if st.session_state.show_dashboard:
            if not st.session_state.selected_room_names:
                st.warning("最低1つのルームを選択してください。")
                return

            st.markdown("<h2 style='font-size:2em;'>3. リアルタイムダッシュボード</h2>", unsafe_allow_html=True)

            # 自動更新コントロール（追加）
            #st.info("7秒ごとに自動更新されます。※停止ボタン押下時は停止します。")
            #toggle_label = "自動更新を停止" if st.session_state.auto_refresh_enabled else "自動更新を再開"
            #if st.button(toggle_label):
            #    st.session_state.auto_refresh_enabled = not st.session_state.auto_refresh_enabled
            #    st.rerun()  # ← experimental_rerunではなくrerun

            with st.container(border=True):
                        col1, col2 = st.columns([1, 1])
                        with col1:
                            st.components.v1.html(f"""
                            <div style="font-weight: bold; font-size: 1.5rem; color: #333333; line-height: 1.2; padding-bottom: 15px;">イベント期間</div>
                            <div style="font-weight: bold; font-size: 1.1rem; color: #333333; line-height: 1.2;">{event_period_str}</div>
                            """, height=80)
                        with col2:
                            st.components.v1.html(f"""
                            <div style="font-weight: bold; font-size: 1.5rem; color: #333333; line-height: 1.2; padding-bottom: 15px;">残り時間</div>
                            <div style="font-weight: bold; font-size: 1.1rem; line-height: 1.2;">
                                <span id="sr_countdown_timer_in_col" style="color: #4CAF50;" data-end="{int(ended_at_dt.timestamp() * 1000)}">計算中...</span>
                            </div>
                            </div>
                            <script>
                            (function() {{
                                function start() {{
                                    const timer = document.getElementById('sr_countdown_timer_in_col');
                                    if (!timer) return false;
                                    const END = parseInt(timer.dataset.end, 10);
                                    if (isNaN(END)) return false;
                                    if (window._sr_countdown_interval_in_col) clearInterval(window._sr_countdown_interval_in_col);

                                    function pad(n) {{ return String(n).padStart(2,'0'); }}
                                    function formatMs(ms) {{
                                        if (ms < 0) ms = 0;
                                        let s = Math.floor(ms / 1000), days = Math.floor(s / 86400);
                                        s %= 86400;
                                        let hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
                                        if (days > 0) return `${{days}}d ${{pad(hh)}}:${{pad(mm)}}:${{pad(ss)}}`;
                                        return `${{pad(hh)}}:${{pad(mm)}}:${{pad(ss)}}`;
                                    }}
                                    function update() {{
                                        const diff = END - Date.now();
                                        if (diff <= 0) {{
                                            timer.textContent = 'イベント終了';
                                            timer.style.color = '#808080';
                                            clearInterval(window._sr_countdown_interval_in_col);
                                            return;
                                        }}
                                        timer.textContent = formatMs(diff);
                                        const totalSeconds = Math.floor(diff / 1000);
                                        if (totalSeconds <= 3600) timer.style.color = '#ff4b4b';
                                        else if (totalSeconds <= 10800) timer.style.color = '#ffa500';
                                        else timer.style.color = '#4CAF50';
                                    }}
                                    update();
                                    window._sr_countdown_interval_in_col = setInterval(update, 1000);
                                    return true;
                                }}
                                let retries = 0;
                                const retry = () => {{
                                    if (window._sr_countdown_interval_in_col || retries++ > 10) return;
                                    if (!start()) setTimeout(retry, 300);
                                }};
                                if (document.readyState === 'complete' || document.readyState === 'interactive') retry();
                                else window.addEventListener('load', retry);
                            }})();
                            </script>
                            """, height=80)


            current_time = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            st.write(f"最終更新日時 (日本時間): {current_time}")

            is_event_ended = datetime.datetime.now(JST) > ended_at_dt
            is_closed = selected_event_data.get('is_closed', True)
            is_aggregating = is_event_ended and not is_closed

            final_ranking_data = {}
            if is_event_ended:
                with st.spinner('イベント終了後の最終ランキングデータを取得中...'):
                    event_url_key = selected_event_data.get('event_url_key')
                    event_id = selected_event_data.get('event_id')
                    final_ranking_map = get_event_ranking_with_room_id(event_url_key, event_id, max_pages=30)
                    if final_ranking_map:
                        for name, data in final_ranking_map.items():
                            if 'room_id' in data:
                                final_ranking_data[data['room_id']] = {
                                    'rank': data.get('rank'), 'point': data.get('point')
                                }
                    else:
                        st.warning("イベント終了後の最終ランキングデータを取得できませんでした。")

            onlives_rooms = get_onlives_rooms()

            data_to_display = []

            is_block_event = selected_event_data.get("is_event_block", False)
            block_event_ranks = {}
            if is_block_event and not is_event_ended:
                with st.spinner('ブロックイベントの全体順位を取得中...'):
                    block_event_ranks = get_block_event_overall_ranking(
                        selected_event_data.get('event_url_key'),
                        event_id=selected_event_data.get('event_id')
                    )

            if st.session_state.selected_room_names:
                premium_live_rooms = [
                    name for name in st.session_state.selected_room_names
                    if st.session_state.room_map_data and name in st.session_state.room_map_data and
                    int(st.session_state.room_map_data[name]['room_id']) in onlives_rooms and
                    onlives_rooms.get(int(st.session_state.room_map_data[name]['room_id']), {}).get('premium_room_type') == 1
                ]

                if premium_live_rooms:
                    room_names_str = '、'.join([f"'{name}'" for name in premium_live_rooms])
                    st.info(f"{room_names_str} は、プレミアムライブのため、ポイントおよびスペシャルギフト履歴情報は取得できません。")

                for room_name in st.session_state.selected_room_names:
                    try:
                        if room_name not in st.session_state.room_map_data:
                            st.error(f"選択されたルーム名 '{room_name}' が見つかりません。リストを更新してください。")
                            continue

                        room_id = st.session_state.room_map_data[room_name]['room_id']
                        rank, point, upper_gap, lower_gap = 'N/A', 'N/A', 'N/A', 'N/A'

                        is_live = int(room_id) in onlives_rooms
                        is_premium_live = False
                        if is_live:
                            live_info = onlives_rooms.get(int(room_id))
                            if live_info and live_info.get('premium_room_type') == 1:
                                is_premium_live = True

                        if is_premium_live:
                            rank = st.session_state.room_map_data[room_name].get('rank')

                            started_at_str = ""
                            if is_live:
                                started_at_ts = onlives_rooms.get(int(room_id), {}).get('started_at')
                                if started_at_ts:
                                    started_at_dt = datetime.datetime.fromtimestamp(started_at_ts, JST)
                                    started_at_str = started_at_dt.strftime("%Y/%m/%d %H:%M")

                            data_to_display.append({
                                "配信中": "🔴",
                                "ルーム名": room_name,
                                "現在の順位": rank,
                                "現在のポイント": "N/A",
                                "上位とのポイント差": "N/A",
                                "下位とのポイント差": "N/A",
                                "配信開始時間": started_at_str
                            })
                            continue

                        if is_event_ended:
                            if room_id in final_ranking_data:
                                rank = final_ranking_data[room_id].get('rank', 'N/A')
                                point = final_ranking_data[room_id].get('point', 'N/A')
                                upper_gap, lower_gap = 0, 0
                            else:
                                st.warning(f"ルーム名 '{room_name}' の最終ランキング情報が見つかりませんでした。")
                                continue
                        else:
                            room_info = get_room_event_info(room_id)
                            if not isinstance(room_info, dict):
                                st.warning(f"ルームID {room_id} のデータが不正な形式です。スキップします。")
                                continue

                            rank_info = None
                            if 'ranking' in room_info and isinstance(room_info['ranking'], dict):
                                rank_info = room_info['ranking']
                            elif 'event_and_support_info' in room_info and isinstance(room_info['event_and_support_info'], dict):
                                event_info = room_info['event_and_support_info']
                                if 'ranking' in event_info and isinstance(event_info['ranking'], dict):
                                    rank_info = event_info['ranking']
                            elif 'event' in room_info and isinstance(room_info['event'], dict):
                                event_data = room_info['event']
                                if 'ranking' in event_data and isinstance(event_data['ranking'], dict):
                                    rank_info = event_data['ranking']

                            if rank_info and 'point' in rank_info:
                                point = rank_info.get('point', 'N/A')
                                upper_gap = rank_info.get('upper_gap', 'N/A')
                                lower_gap = rank_info.get('lower_gap', 'N/A')

                                if is_block_event:
                                    rank = block_event_ranks.get(room_id, 'N/A')
                                else:
                                    rank = rank_info.get('rank', 'N/A')
                            else:
                                st.warning(f"ルーム名 '{room_name}' のランキング情報が不完全です。スキップします。")
                                continue

                        started_at_str = ""
                        if is_live:
                            started_at_ts = onlives_rooms.get(int(room_id), {}).get('started_at')
                            if started_at_ts:
                                started_at_dt = datetime.datetime.fromtimestamp(started_at_ts, JST)
                                started_at_str = started_at_dt.strftime("%Y/%m/%d %H:%M")

                        data_to_display.append({
                            "配信中": "🔴" if is_live else "", "ルーム名": room_name,
                            "現在の順位": rank, "現在のポイント": point,
                            "上位とのポイント差": upper_gap, "下位とのポイント差": lower_gap,
                            "配信開始時間": started_at_str
                        })
                    except Exception as e:
                        st.error(f"データ処理中に予期せぬエラーが発生しました（ルーム名: {room_name}）。エラー: {e}")
                        continue

            if data_to_display:
                df = pd.DataFrame(data_to_display)

                # --- 新：数値列の準備（ポイントの数値列を保持して計算に使用） ---
                # 元のポイント列は混在するため数値抽出を行う
                df['現在のポイント_numeric'] = pd.to_numeric(df['現在のポイント'], errors='coerce')
                # NaN を 0 にしないでそのままにする（差分計算時は fillna で扱う）
                # 現在の順位は数値化
                df['現在の順位'] = pd.to_numeric(df['現在の順位'], errors='coerce')

                # ブロックイベントか否かでソート方針は従来どおり
                if is_aggregating:
                    # イベント終了後の集計中表示だが、ポイント自体は表示する（xxxxxxx（※集計中））
                    # 順位ソート（ブロックイベントは has_valid_rank 優先）
                    if is_block_event:
                        df['has_valid_rank'] = df['現在の順位'] > 0
                        df = df.sort_values(by=['has_valid_rank', '現在の順位'], ascending=[False, True], na_position='last').reset_index(drop=True)
                        df = df.drop(columns=['has_valid_rank'])
                    else:
                        df = df.sort_values(by='現在の順位', ascending=True, na_position='last').reset_index(drop=True)

                    # ポイント差を算出（数値列を用いる）
                    df_sorted_by_points = df.sort_values(by='現在のポイント_numeric', ascending=False, na_position='last').reset_index(drop=True)
                    df_sorted_by_points['上位とのポイント差'] = (df_sorted_by_points['現在のポイント_numeric'].shift(1) - df_sorted_by_points['現在のポイント_numeric']).abs().fillna(0).astype(int)
                    df_sorted_by_points['下位とのポイント差'] = (df_sorted_by_points['現在のポイント_numeric'].shift(-1) - df_sorted_by_points['現在のポイント_numeric']).abs().fillna(0).astype(int)

                    # merge して差分列を戻す
                    df = pd.merge(df.drop(columns=['上位とのポイント差', '下位とのポイント差'], errors='ignore'), df_sorted_by_points[['ルーム名', '上位とのポイント差', '下位とのポイント差']], on='ルーム名', how='left')

                    # 表示用ポイント列を作成（カンマ区切り + 集計中注記）
                    def fmt_agg(x):
                        try:
                            if pd.isna(x):
                                return "（※集計中）"
                            return f"{int(x):,}（※集計中）"
                        except:
                            return "（※集計中）"
                    df['現在のポイント_display'] = df['現在のポイント_numeric'].apply(fmt_agg)
                    # UI 表示列に置き換え（計算用の numeric 列は残す）
                    df['現在のポイント'] = df['現在のポイント_display']
                    df = df.drop(columns=['現在のポイント_display'])

                    # 差分は数値列のままにしておく（後でスタイルで桁区切り）
                    df['上位とのポイント差'] = df['上位とのポイント差'].fillna(0).astype(int)
                    df['下位とのポイント差'] = df['下位とのポイント差'].fillna(0).astype(int)

                    # 配信開始時間のカラム位置調整（従来どおり）
                    started_at_column = df['配信開始時間']
                    df = df.drop(columns=['配信開始時間'])
                    df.insert(1, '配信開始時間', started_at_column)

                else:
                    # 集計前（通常表示）: 数値化してソート・差分算出（従来のロジック）
                    df['現在のポイント'] = pd.to_numeric(df['現在のポイント'], errors='coerce')

                    if is_event_ended or is_block_event: # ブロックイベントも順位でソート
                        df['has_valid_rank'] = df['現在の順位'] > 0
                        df = df.sort_values(by=['has_valid_rank', '現在の順位'], ascending=[False, True], na_position='last').reset_index(drop=True)
                        df = df.drop(columns=['has_valid_rank'])
                    else:
                        df = df.sort_values(by='現在の順位', ascending=True, na_position='last').reset_index(drop=True)

                    live_status = df['配信中']
                    df = df.drop(columns=['配信中'])

                    df_sorted_by_points = df.sort_values(by='現在のポイント', ascending=False, na_position='last').reset_index(drop=True)
                    df_sorted_by_points['上位とのポイント差'] = (df_sorted_by_points['現在のポイント'].shift(1) - df_sorted_by_points['現在のポイント']).abs().fillna(0).astype(int)
                    df_sorted_by_points['下位とのポイント差'] = (df_sorted_by_points['現在のポイント'].shift(-1) - df_sorted_by_points['現在のポイント']).abs().fillna(0).astype(int)

                    df = pd.merge(df.drop(columns=['上位とのポイント差', '下位とのポイント差'], errors='ignore'), df_sorted_by_points[['ルーム名', '上位とのポイント差', '下位とのポイント差']], on='ルーム名', how='left')

                    df.insert(0, '配信中', live_status)

                    started_at_column = df['配信開始時間']
                    df = df.drop(columns=['配信開始時間'])
                    df.insert(1, '配信開始時間', started_at_column)

                # ---- 表示（スタイル適用） ----
                st.markdown(
                    """
                    <style>
                    h3.custom-status-title {
                        padding-top: 0 !important;
                        padding-bottom: 0px !important;
                        margin: 0 !important;
                    }
                    </style>
                    """,
                    unsafe_allow_html=True
                )
                st.markdown(
                    "<h3 class='custom-status-title'>📊 比較対象ルームのステータス</h3>",
                    unsafe_allow_html=True
                )

                required_cols = ['現在のポイント', '上位とのポイント差', '下位とのポイント差']
                if all(col in df.columns for col in required_cols):
                    try:
                        # 表示用: numeric列は削除
                        display_df = df.drop(columns=['現在のポイント_numeric'], errors='ignore')

                        # 行の背景色ハイライト関数
                        def highlight_rows(row):
                            if row['配信中'] == '🔴':
                                return ['background-color: #e6fff2'] * len(row)
                            elif row.name % 2 == 1:
                                return ['background-color: #fcfcfc'] * len(row)
                            else:
                                return [''] * len(row)

                        df_to_format = df.copy()

                        # 集計中ポイントも右寄せを強制
                        st.markdown(
                            """
                            <style>
                            div[data-testid="stDataFrame"] td {
                                text-align: right !important;
                            }
                            div[data-testid="stDataFrame"] th {
                                text-align: center !important;
                            }
                            </style>
                            """,
                            unsafe_allow_html=True
                        )

                        if not is_aggregating:
                            # ✅ 通常時: ヘッダーはそのまま、セルは数値＋カンマ区切り
                            for col in ['現在のポイント', '上位とのポイント差', '下位とのポイント差']:
                                df_to_format[col] = pd.to_numeric(df_to_format[col], errors='coerce').fillna(0).astype(int)

                            styled_df = (
                                df_to_format.drop(columns=['現在のポイント_numeric'], errors='ignore')
                                .style.apply(highlight_rows, axis=1)
                                .format({
                                    '現在のポイント': '{:,}',
                                    '上位とのポイント差': '{:,}',
                                    '下位とのポイント差': '{:,}'
                                })
                                .set_properties(subset=['現在のポイント','上位とのポイント差','下位とのポイント差'],
                                                **{'text-align': 'right'})
                            )

                        else:
                            st.markdown("<span style='color:red; font-weight:bold;'>※ポイントは集計中です</span>", unsafe_allow_html=True)
                            # ✅ 集計中: ヘッダーを「現在のポイント（※集計中）」に変更し、セルには数値のみを表示
                            df_to_format = df.copy()
                            df_to_format.rename(columns={'現在のポイント': '現在のポイント'}, inplace=True)

                            # 数値部分を抽出（既存の numeric 列を使用）
                            df_to_format['現在のポイント'] = df['現在のポイント_numeric'].apply(lambda x: int(x) if pd.notnull(x) else 0)

                            styled_df = (
                                df_to_format.drop(columns=['現在のポイント_numeric'], errors='ignore')
                                .style.apply(highlight_rows, axis=1)
                                .format({
                                    '現在のポイント': '{:,}',
                                    '上位とのポイント差': '{:,}',
                                    '下位とのポイント差': '{:,}'
                                })
                                .set_properties(subset=['現在のポイント','上位とのポイント差','下位とのポイント差'],
                                                **{'text-align': 'right'})
                            )

                        #st.markdown("<span style='color:red; font-weight:bold;'>※集計中のポイントです</span>", unsafe_allow_html=True)
                        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=265)

                    except Exception as e:
                        st.error(f"データフレームのスタイル適用中にエラーが発生しました: {e}")
                        st.dataframe(df, use_container_width=True, hide_index=True, height=265)
                else:
                    st.dataframe(df, use_container_width=True, hide_index=True, height=265)

            st.markdown("<div style='margin-bottom: 16px;'></div>", unsafe_allow_html=True)
            gift_history_title = "🎁 スペシャルギフト履歴"
            if is_event_ended:
                gift_history_title += " <span style='font-size: 14px;'>（イベントは終了しましたが、現在配信中のルームのみ表示）</span>"
            else:
                gift_history_title += " <span style='font-size: 14px;'>（現在配信中のルームのみ表示）</span>"
            st.markdown(f"### {gift_history_title}", unsafe_allow_html=True)

            gift_container = st.container()        
            css_style = """
                <style>
                .container-wrapper { display: flex; flex-wrap: wrap; gap: 15px; }
                .room-container {
                    position: relative; width: 169px; flex-shrink: 0; border: 1px solid #ddd; border-radius: 5px;
                    padding: 10px; height: 500px; display: flex; flex-direction: column; padding-top: 30px; margin-top: 16px;
                    margin-bottom: 16px;
                }
                .ranking-label {
                    position: absolute; top: -12px; left: 50%; transform: translateX(-50%); padding: 2px 8px;
                    border-radius: 12px; color: white; font-weight: bold; font-size: 0.9rem; z-index: 10;
                    white-space: nowrap; box-shadow: 0 2px 5px rgba(0,0,0,0.2);
                }
                .room-title {
                    text-align: center; font-size: 1rem; font-weight: bold; margin-bottom: 10px; display: -webkit-box;
                    -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; white-space: normal;
                    line-height: 1.4em; min-height: calc(1.4em * 3);
                }
                .gift-list-container { flex-grow: 1; height: 400px; overflow-y: scroll; scrollbar-width: auto; }
                .gift-item { display: flex; flex-direction: column; padding: 8px 8px; border-bottom: 1px solid #eee; gap: 4px; }
                .gift-item:last-child { border-bottom: none; }
                .gift-header { font-weight: bold; }
                .gift-info-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
                .gift-image { width: 30px; height: 30px; border-radius: 5px; object-fit: contain; }
                .highlight-10000 { background-color: #ffe5e5; } .highlight-30000 { background-color: #ffcccc; }
                .highlight-60000 { background-color: #ffb2b2; } .highlight-100000 { background-color: #ff9999; }
                .highlight-300000 { background-color: #ff7f7f; }
                </style>
            """

            live_rooms_data = []
            if 'df' in locals() and not df.empty and st.session_state.room_map_data:
                selected_live_room_ids = {
                    int(st.session_state.room_map_data[row['ルーム名']]['room_id']) for index, row in df.iterrows() 
                    if '配信中' in row and row['配信中'] == '🔴' and onlives_rooms.get(int(st.session_state.room_map_data[row['ルーム名']]['room_id']), {}).get('premium_room_type') != 1
                }
                rooms_to_delete = [room_id for room_id in st.session_state.gift_log_cache if int(room_id) not in selected_live_room_ids]
                for room_id in rooms_to_delete:
                    del st.session_state.gift_log_cache[room_id]

                for index, row in df.iterrows():
                    room_name = row['ルーム名']
                    if room_name in st.session_state.room_map_data:
                        room_id = st.session_state.room_map_data[room_name]['room_id']
                        if int(room_id) in onlives_rooms:
                            if onlives_rooms.get(int(room_id), {}).get('premium_room_type') != 1:
                                live_rooms_data.append({
                                    "room_name": room_name, "room_id": room_id, "rank": row['現在の順位']
                                })
                            else:
                                live_rooms_data.append({
                                    "room_name": room_name, "room_id": room_id, "rank": row['現在の順位']
                                })

            room_html_list = []
            if len(live_rooms_data) > 0:
                for room_data in live_rooms_data:
                    room_name = room_data['room_name']
                    room_id = room_data['room_id']
                    rank = room_data.get('rank', 'N/A')
                    rank_color = get_rank_color(rank)

                    if onlives_rooms.get(int(room_id), {}).get('premium_room_type') == 1:
                        html_content = f"""
                        <div class="room-container">
                            <div class="ranking-label" style="background-color: {rank_color};">{rank}位</div>
                            <div class="room-title">{room_name}</div>
                            <div class="gift-list-container">
                                <p style="text-align: center; padding: 12px 0; color: orange; font-size:12px;">プレミアムライブのため<br>ギフト情報取得不可</p>
                            </div>
                        </div>
                        """
                        room_html_list.append(html_content)
                        continue

                    if int(room_id) in onlives_rooms:
                        gift_log = get_and_update_gift_log(room_id)
                        gift_list_map = get_gift_list(room_id)

                        html_content = f"""
                        <div class="room-container">
                            <div class="ranking-label" style="background-color: {rank_color};">{rank}位</div>
                            <div class="room-title">{room_name}</div>
                            <div class="gift-list-container">
                        """
                        if not gift_list_map:
                            html_content += '<p style="text-align: center; padding: 12px 0; color: orange;">ギフト情報取得失敗</p>'

                        if gift_log:
                            for log in gift_log:
                                gift_id = log.get('gift_id')
                                gift_info = gift_list_map.get(str(gift_id), {})
                                gift_point = gift_info.get('point', 0)
                                gift_count = log.get('num', 0)
                                total_point = gift_point * gift_count
                                highlight_class = ""
                                if gift_point >= 500:
                                    if total_point >= 300000: highlight_class = "highlight-300000"
                                    elif total_point >= 100000: highlight_class = "highlight-100000"
                                    elif total_point >= 60000: highlight_class = "highlight-60000"
                                    elif total_point >= 30000: highlight_class = "highlight-30000"
                                    elif total_point >= 10000: highlight_class = "highlight-10000"

                                gift_image = log.get('image', gift_info.get('image', ''))
                                html_content += (
                                    f'<div class="gift-item {highlight_class}">'
                                    f'<div class="gift-header"><small>{datetime.datetime.fromtimestamp(log.get("created_at", 0), JST).strftime("%H:%M:%S")}</small></div>'
                                    f'<div class="gift-info-row"><img src="{gift_image}" class="gift-image" /><span>×{gift_count}</span></div>'
                                    f'<div>{gift_point}pt</div></div>'
                                )
                            html_content += '</div>'
                        else:
                            html_content += '<p style="text-align: center; padding: 12px 0;">ギフト履歴がありません。</p></div>'

                        html_content += '</div>'
                        room_html_list.append(html_content)
                html_container_content = '<div class="container-wrapper">' + ''.join(room_html_list) + '</div>'
                gift_container.markdown(css_style + html_container_content, unsafe_allow_html=True)
            else:
                gift_container.info("選択されたルームに現在配信中のルームはありません。")

            st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)


            # --- ここから「戦闘モード！」修正版（変更点：ポイント取得時に表示文字列→数値を抽出する耐性を付与） ---
            st.markdown("### ⚔ 必要ギフト数簡易算出", unsafe_allow_html=True)

            if 'df' in locals() and not df.empty and 'ルーム名' in df.columns:
                room_options_all = df['ルーム名'].tolist()
            else:
                room_options_all = list(st.session_state.room_map_data.keys()) if st.session_state.room_map_data else []

            if not room_options_all:
                st.info("比較対象ルームが見つかりません。")
            else:
                room_rank_map = {}
                df_rank_map = {}
                if 'df' in locals() and not df.empty and 'ルーム名' in df.columns and '現在の順位' in df.columns:
                    for _, row in df.iterrows():
                        if pd.notna(row['現在の順位']):
                            try:
                                df_rank_map[row['ルーム名']] = int(row['現在の順位'])
                            except:
                                df_rank_map[row['ルーム名']] = row['現在の順位']

                for rn in room_options_all:
                    if rn in df_rank_map:
                        rank_display = f"{df_rank_map[rn]}位"
                    else:
                        raw_rank = st.session_state.room_map_data.get(rn, {}).get("rank")
                        try:
                            rank_int = int(raw_rank)
                            rank_display = f"{rank_int}位" if rank_int > 0 else "N/A"
                        except:
                            rank_display = "N/A"
                    room_rank_map[rn] = f"{rank_display}：{rn}"

                # 🔽 現在のルーム順位情報をもとに並び替え（昇順＝上位が先）
                sorted_rooms = sorted(
                    room_options_all,
                    key=lambda r: df_rank_map.get(r, float('inf'))
                )

                # ▼ デフォルト対象・ターゲット設定
                default_target_room = None
                default_enemy_room = None

                if len(sorted_rooms) >= 2:
                    # 対象: 上位から2番目
                    default_target_room = sorted_rooms[1]
                    # ターゲット: 上位から2番目を除く上位ルーム群（上位ルームを先に表示）
                    default_enemy_room = sorted_rooms[0]
                elif len(sorted_rooms) == 1:
                    default_target_room = sorted_rooms[0]
                    default_enemy_room = None

                col_a, col_b = st.columns([1, 1])
                with col_a:
                    selected_target_room = st.selectbox(
                        "対象ルームを選択:",
                        room_options_all,
                        index=room_options_all.index(default_target_room) if default_target_room in room_options_all else 0,
                        format_func=lambda x: room_rank_map.get(x, x),
                        key="battle_target_room"
                    )

                with col_b:
                    other_rooms = [r for r in room_options_all if r != selected_target_room]
                    selected_enemy_room = st.selectbox(
                        "ターゲットルームを選択:",
                        other_rooms,
                        index=other_rooms.index(default_enemy_room) if default_enemy_room in other_rooms else 0,
                        format_func=lambda x: room_rank_map.get(x, x),
                        key="battle_enemy_room"
                    ) if other_rooms else None

                points_map = {}
                try:
                    if 'df' in locals() and not df.empty:
                        for _, r in df.iterrows():
                            rn = r.get('ルーム名')
                            pval = r.get('現在のポイント')
                            parsed = extract_int_from_mixed(pval)
                            if parsed is not None:
                                points_map[rn] = int(parsed)
                            else:
                                # fallback
                                try:
                                    points_map[rn] = int(st.session_state.room_map_data.get(rn, {}).get('point', 0) or 0)
                                except:
                                    points_map[rn] = 0
                    else:
                        for rn, info in st.session_state.room_map_data.items():
                            points_map[rn] = int(info.get('point', 0) or 0)
                except:
                    for rn, info in st.session_state.room_map_data.items():
                        points_map[rn] = int(info.get('point', 0) or 0)

                if selected_enemy_room:
                    target_point = points_map.get(selected_target_room, 0)
                    enemy_point = points_map.get(selected_enemy_room, 0)
                    diff = target_point - enemy_point
                    if enemy_point == target_point:
                        needed = 0
                    else:
                        needed_points_to_overtake = max(0, enemy_point - target_point + 1)
                        needed = max(0, needed_points_to_overtake)

                    target_rank = None
                    target_lower_gap = None
                    try:
                        if 'df' in locals() and not df.empty and 'ルーム名' in df.columns:
                            row = df[df['ルーム名'] == selected_target_room]
                            if not row.empty:
                                if not pd.isna(row.iloc[0].get('現在の順位')):
                                    target_rank = int(row.iloc[0].get('現在の順位'))
                                if '下位とのポイント差' in row.columns:
                                    lg = row.iloc[0].get('下位とのポイント差')
                                    if not pd.isna(lg):
                                        target_lower_gap = int(lg)
                    except:
                        pass
                    if target_rank is None:
                        target_rank = st.session_state.room_map_data.get(selected_target_room, {}).get('rank')

                    lower_gap_text = (
                        f"※下位とのポイント差: {target_lower_gap:,} pt"
                        if target_lower_gap is not None
                        else "※下位とのポイント差: N/A"
                    )

                    if diff > 0:
                        st.markdown(
                            f"<div style='background-color:#d4edda; padding:16px; border-radius:8px; margin-bottom:5px;'>"
                            f"<span style='font-size:1.6rem; font-weight:bold; color:#155724;'>{abs(diff):,}</span> pt <span style='font-size:1.2rem; font-weight:bold; color:#155724;'>リード</span>しています"
                            f"（対象: {target_point:,} pt / ターゲット: {enemy_point:,} pt）。 {lower_gap_text}</div>",
                            unsafe_allow_html=True
                        )
                    elif diff < 0:
                        st.markdown(
                            f"<div style='background-color:#fff3cd; padding:16px; border-radius:8px; margin-bottom:5px;'>"
                            f"<span style='font-size:1.6rem; font-weight:bold; color:#856404;'>{abs(diff):,}</span> pt <span style='font-size:1.2rem; font-weight:bold; color:#856404;'>ビハインド</span>です"
                            f"（対象: {target_point:,} pt / ターゲット: {enemy_point:,} pt）。 {lower_gap_text}</div>",
                            unsafe_allow_html=True
                        )
                    else:
                        st.markdown(
                            f"<div style='background-color:#d1ecf1; padding:16px; border-radius:8px; margin-bottom:5px;'>"
                            f"ポイントは<span style='font-size:1.2rem; font-weight:bold; color:#0c5460;'>同点</span>です（<span style='font-size:1.6rem; font-weight:bold; color:#0c5460;'>{target_point:,}</span> pt）。 {lower_gap_text}</div>",
                            unsafe_allow_html=True
                        )

                    st.markdown(f"- 対象ルームの現在順位: **{target_rank if target_rank is not None else 'N/A'}位**")

                    large_sg = [500, 1000, 3000, 10000, 20000, 100000]
                    small_sg = [1, 2, 3, 5, 8, 10, 50, 88, 100, 200]
                    rainbow_pt = 100 * 2.5
                    rainbow10_pt = 100 * 10 * 1.20 * 2.5
                    big_rainbow_pt = 1250 * 1.20 * 2.5
                    rainbow_meteor_pt = 2500 * 1.20 * 2.5

                    if enemy_point == target_point:
                        needed = 0
                    else:
                        needed_points_to_overtake = max(0, enemy_point - target_point + 1)
                        needed = max(0, needed_points_to_overtake)

                    large_table = {
                        "ギフト種類": [f"{sg}G" for sg in large_sg],
                        "必要個数 (小数2桁)": [f"{needed/(sg*3):.2f}" if sg > 0 else "0.00" for sg in large_sg]
                    }
                    small_table = {
                        "ギフト種類": [f"{sg}G" for sg in small_sg],
                        "必要個数 (小数2桁)": [f"{needed/(sg*2.5):.2f}" if sg > 0 else "0.00" for sg in small_sg]
                    }
                    rainbow_table = {
                        "ギフト種類": ["レインボースター 100pt", "レインボースター 100pt × 10連", "大レインボースター 1250pt", "レインボースター流星群 2500pt"],
                        "必要個数 (小数2桁)": [
                            f"{needed/rainbow_pt:.2f}",
                            f"{needed/rainbow10_pt:.2f}",
                            f"{needed/big_rainbow_pt:.2f}",
                            f"{needed/rainbow_meteor_pt:.2f}"
                        ]
                    }

                    st.markdown(
                        """
                        <div style='margin-bottom:2px;'>
                          <span style='font-size:1.4rem; font-weight:bold; display:inline-block; line-height:1.6;'>
                            ▼必要なギフト例<span style='font-size: 14px;'>（有償SG&レインボースター）</span>
                          </span>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                    def df_to_html_table(df):
                        html = df.to_html(index=False, justify="center", border=0, classes="gift-table")
                        style = """
                        <style>
                        table.gift-table {
                            border-collapse: collapse;
                            width: 100%;
                            font-size: 0.9rem;
                            line-height: 1.3;
                            margin-top: 0;
                        }
                        table.gift-table th {
                            background-color: #f1f3f4;
                            color: #333;
                            padding: 6px 8px;
                            border-bottom: 1px solid #ccc;
                            font-weight: 600;
                        }
                        table.gift-table td {
                            padding: 5px 8px;
                            border-bottom: 1px solid #e0e0e0;
                        }
                        table.gift-table tbody tr:nth-child(even) {
                            background-color: #fafafa;
                        }
                        </style>
                        """
                        return style + html

                    large_html = f"<h4 style='font-size:1.2em; margin-top:0;'>有償SG（500G以上）</h4>{df_to_html_table(pd.DataFrame(large_table))}"
                    small_html = f"<h4 style='font-size:1.2em; margin-top:0;'>有償SG（500G未満）<span style='font-size: 14px;'>※連打考慮外</span></h4>{df_to_html_table(pd.DataFrame(small_table))}"
                    rainbow_html = f"<h4 style='font-size:1.2em; margin-top:0;'>レインボースター系<span style='font-size: 14px;'>  ※連打考慮外</span></h4>{df_to_html_table(pd.DataFrame(rainbow_table))}"

                    container_html = f"""
                    <div style='border:2px solid #ccc; border-radius:12px; padding:12px 16px 16px 16px; background-color:#fdfdfd; margin-top:4px;'>
                      <div style='display:flex; justify-content:space-between; gap:16px;'>
                        <div style='flex:1;'>{large_html}</div>
                        <div style='flex:1;'>{small_html}</div>
                        <div style='flex:1;'>{rainbow_html}</div>
                      </div>
                    </div>
                    """

                    st.markdown(container_html, unsafe_allow_html=True)
                else:
                    st.info("ターゲットルームを選択してください。")
            # --- ここまで戦闘モード修正版 ---

            st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)
            st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)

            st.markdown(
                """
                <style>
                h3.custom-status-title2 {
                    padding-top: 0 !important;
                    padding-bottom: 0px !important;
                    margin: 0 !important;
                }
                </style>
                """,
                unsafe_allow_html=True
            )
            st.markdown(
                "<h3 class='custom-status-title2'>📈 ポイントと順位の比較</h3>",
                unsafe_allow_html=True
            )
            #st.markdown("### 📈 ポイントと順位の比較", unsafe_allow_html=True)

            #if not is_aggregating and 'df' in locals() and not df.empty:
            if 'df' in locals() and not df.empty:
                color_map = {row['ルーム名']: get_rank_color(row['現在の順位']) for index, row in df.iterrows()}
                points_container = st.container()

                with points_container:
                    if '現在のポイント' in df.columns:
                        # ✅ 集計中かどうかで使う列を切り替える
                        y_col = "現在のポイント_numeric" if is_aggregating else "現在のポイント"
                        fig_points = px.bar(
                            df, x="ルーム名", y=y_col, title="各ルームの現在のポイント", color="ルーム名",
                            color_discrete_map=color_map, hover_data=["現在の順位", "上位とのポイント差", "下位とのポイント差"],
                            labels={y_col: "ポイント", "ルーム名": "ルーム名"}
                        )
                        st.plotly_chart(fig_points, use_container_width=True, key="points_chart")
                        fig_points.update_layout(uirevision="const")

                    if len(st.session_state.selected_room_names) > 1 and "上位とのポイント差" in df.columns:
                        df['上位とのポイント差'] = pd.to_numeric(df['上位とのポイント差'], errors='coerce')
                        fig_upper_gap = px.bar(
                            df, x="ルーム名", y="上位とのポイント差", title="上位とのポイント差", color="ルーム名",
                            color_discrete_map=color_map, hover_data=["現在の順位", "現在のポイント"],
                            labels={"上位とのポイント差": "ポイント差", "ルーム名": "ルーム名"}
                        )
                        st.plotly_chart(fig_upper_gap, use_container_width=True, key="upper_gap_chart")
                        fig_upper_gap.update_layout(uirevision="const")

                    if len(st.session_state.selected_room_names) > 1 and "下位とのポイント差" in df.columns:
                        df['下位とのポイント差'] = pd.to_numeric(df['下位とのポイント差'], errors='coerce')
                        fig_lower_gap = px.bar(
                            df, x="ルーム名", y="下位とのポイント差", title="下位とのポイント差", color="ルーム名",
                            color_discrete_map=color_map, hover_data=["現在の順位", "現在のポイント"],
                            labels={"下位とのポイント差": "ポイント差", "ルーム名": "ルーム名"}
                        )
                        st.plotly_chart(fig_lower_gap, use_container_width=True, key="lower_gap_chart")
                        fig_lower_gap.update_layout(uirevision="const")
            else:
                #st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)
                #st.info("ポイント集計中のためグラフは表示されません。")
                pass


            # 自動更新（7秒ごと）
            st_autorefresh(interval=7000, limit=None, key="refresh")


if __name__ == "__main__":
    main()