import os, sys, time, json, argparse
import random
import requests
import gspread
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential_jitter
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

API_BASE = "https://graph.threads.net/v1.0"
TZ = ZoneInfo("Asia/Tokyo")
NEED_COLS = ["text","image_url","alt_text","link_attachment","reply_control","topic_tag","location_id","status","posted_at","error"]

def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}

def _service_account_path():
    return (os.environ.get("GSPREAD_SERVICE_ACCOUNT_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.path.expanduser("~/.config/gspread/service_account.json"))

def load_env():
    load_dotenv()
    user_id=os.getenv("THREADS_USER_ID","").strip()
    token=os.getenv("THREADS_ACCESS_TOKEN","").strip()
    sheet_url=os.getenv("SHEET_URL","").strip()
    sheet_tab=os.getenv("SHEET_TAB","").strip()
    if not user_id or not token:
        print("THREADS_USER_ID or THREADS_ACCESS_TOKEN missing in .env", file=sys.stderr); sys.exit(2)
    if not sheet_url:
        print("SHEET_URL missing in .env", file=sys.stderr); sys.exit(2)
    return user_id, token, sheet_url, sheet_tab

def gs_open(sheet_url, sheet_tab):
    sa=_service_account_path()
    gc=gspread.service_account(filename=sa) if os.path.exists(sa) else gspread.service_account()
    sh=gc.open_by_url(sheet_url)
    ws=sh.worksheet(sheet_tab) if sheet_tab else sh.sheet1
    return ws

def ensure_header(ws):
    values=ws.get_all_values()
    if not values:
        ws.update(values=[NEED_COLS], range_name="1:1"); return NEED_COLS
    header=values[0]; changed=False
    for col in NEED_COLS:
        if col not in header:
            header.append(col); changed=True
    if changed:
        ws.update(values=[header], range_name="1:1")
    return header

def rows_with_index(ws, header):
    values=ws.get_all_values()
    if len(values)<=1: return []
    rows=[]
    for i, raw in enumerate(values[1:], start=2):  # sheet row index
        row={h:(raw[idx] if idx<len(raw) else "") for idx,h in enumerate(header)}
        rows.append((i,row))
    return rows

@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 4), reraise=True)
def create_container(user_id, token, payload):
    url=f"{API_BASE}/{user_id}/threads"
    resp=requests.post(url, headers=auth_headers(token), json=payload, timeout=30)
    if resp.status_code>=400: raise Exception(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()

@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 4), reraise=True)
def publish_container(user_id, token, creation_id):
    url=f"{API_BASE}/{user_id}/threads_publish"
    resp=requests.post(url, headers=auth_headers(token), params={"creation_id": creation_id}, timeout=30)
    if resp.status_code>=400: raise Exception(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()

def post_one(user_id, token, row):
    text = (row.get("text") or "").strip()
    image_url = (row.get("image_url") or "").strip()
    if image_url:
        payload={"media_type":"IMAGE","image_url":image_url,"text":text}
        if row.get("alt_text"):       payload["alt_text"]=row["alt_text"]
        if row.get("reply_control"):  payload["reply_control"]=row["reply_control"]
        if row.get("topic_tag"):      payload["topic_tag"]=row["topic_tag"]
        if row.get("location_id"):    payload["location_id"]=row["location_id"]
        data=create_container(user_id, token, payload)
        cid=data.get("id")
        pub=publish_container(user_id, token, cid)
        return {"status":"published","container_id":cid,"media_type":"IMAGE","publish":pub}
    else:
        payload={"media_type":"TEXT","text":text,"auto_publish_text":True}
        if row.get("link_attachment"): payload["link_attachment"]=row["link_attachment"]
        if row.get("reply_control"):   payload["reply_control"]=row["reply_control"]
        if row.get("topic_tag"):       payload["topic_tag"]=row["topic_tag"]
        if row.get("location_id"):     payload["location_id"]=row["location_id"]
        data=create_container(user_id, token, payload)
        return {"status":"published","container_id":data.get("id"),"media_type":"TEXT"}

def update_result(ws, header, row_idx, status, err=""):
    col_idx = {h:i+1 for i,h in enumerate(header)}
    ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    ws.update_cell(row_idx, col_idx["status"], status)
    ws.update_cell(row_idx, col_idx["posted_at"], ts if status=="posted" else "")
    ws.update_cell(row_idx, col_idx["error"], err[:3000] if status=="failed" else "")

def first_unposted(rows):
    skip = {"posted", "posted✅", "done", "済", "hold", "rejected", "error", "failed"}
    for (i,r) in rows:
        if (r.get("text") or r.get("image_url")) and r.get("status","").strip().lower() not in skip:
            return (i,r)
    return None

def post_next_unposted(user_id, token, ws, header):
    rows=rows_with_index(ws, header)
    pick=first_unposted(rows)
    if not pick:
        print(json.dumps({"ok":True,"msg":"no rows to post"}, ensure_ascii=False))
        return False
    row_idx, row = pick
    try:
        res=post_one(user_id, token, row)
        update_result(ws, header, row_idx, "posted", "")
        print(json.dumps({"ok":True,"row_idx":row_idx,"row":row,"res":res}, ensure_ascii=False))
        return True
    except Exception as e:
        err=str(e)
        update_result(ws, header, row_idx, "failed", err)
        print(json.dumps({"ok":False,"row_idx":row_idx,"row":row,"err":err}, ensure_ascii=False))
        return False

# --------- モード実装 ---------

def run_batch(max_per_run=0):
    user_id, token, sheet_url, sheet_tab=load_env()
    ws=gs_open(sheet_url, sheet_tab)
    header=ensure_header(ws)
    rows=rows_with_index(ws, header)
    n=0
    while True:
        pick=first_unposted(rows)
        if not pick: break
        row_idx, row = pick
        try:
            res=post_one(user_id, token, row)
            update_result(ws, header, row_idx, "posted", "")
            print(json.dumps({"ok":True,"row_idx":row_idx,"row":row,"res":res}, ensure_ascii=False))
        except Exception as e:
            update_result(ws, header, row_idx, "failed", str(e))
            print(json.dumps({"ok":False,"row_idx":row_idx,"row":row,"err":str(e)}, ensure_ascii=False))
        n+=1
        if max_per_run and n>=max_per_run: break
        # 次の先頭を見に行く
        rows=rows_with_index(ws, header)

def run_schedule(interval_min=120):
    import schedule
    user_id, token, sheet_url, sheet_tab=load_env()
    ws=gs_open(sheet_url, sheet_tab)
    header=ensure_header(ws)
    def job():
        post_next_unposted(user_id, token, ws, header)
    job()
    schedule.every(interval_min).minutes.do(job)
    while True:
        schedule.run_pending(); time.sleep(1)

# --- 時刻ユーティリティ ---

def _parse_hhmm(s):
    h,m = map(int, s.split(":"))
    return dtime(hour=h, minute=m)

def _parse_hhmm_ext(s):
    # 24:00 や 26:00 を翌日に解釈
    h,m = map(int, s.split(":"))
    extra_days, h = divmod(h, 24)
    return extra_days, dtime(hour=h, minute=m)

def _next_random_in_window(window_str):
    # "HH:MM-HH:MM" のウィンドウ内で次のランダム実行時刻（JST）
    start_s, end_s = window_str.split("-")
    t_start, t_end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    now = datetime.now(TZ)
    today = now.date()
    start_dt = datetime.combine(today, t_start, TZ)
    end_dt   = datetime.combine(today, t_end, TZ)
    if end_dt <= now or end_dt <= start_dt:
        start_dt += timedelta(days=1)
        end_dt   += timedelta(days=1)
    win_start = max(start_dt, now + timedelta(seconds=5))
    if end_dt - win_start <= timedelta(seconds=5):
        start_dt += timedelta(days=1); end_dt += timedelta(days=1); win_start = start_dt
    delta = end_dt - win_start
    rsec = int(delta.total_seconds())
    offs = random.randint(0, max(0, rsec))
    return win_start + timedelta(seconds=offs)

def run_daily_window(window_str):
    user_id, token, sheet_url, sheet_tab=load_env()
    print(f"[scheduler] daily window = {window_str}")
    while True:
        nxt = _next_random_in_window(window_str)
        print(f"[scheduler] next run at {nxt.isoformat()}")
        while True:
            now = datetime.now(TZ)
            if now >= nxt: break
            time.sleep(min(60, max(1,int((nxt-now).total_seconds()))))
        ws=gs_open(sheet_url, sheet_tab)
        header=ensure_header(ws)
        post_next_unposted(user_id, token, ws, header)
        # 次は翌日のウィンドウを抽選
        nxt = _next_random_in_window(window_str)
        nxt = nxt + timedelta(days=1)

def _next_at_with_jitter(time_str, jitter_min, ref=None):
    if ref is None: ref = datetime.now(TZ)
    d, t = _parse_hhmm_ext(time_str)
    base = datetime.combine(ref.date(), t, TZ) + timedelta(days=d)
    start = base - timedelta(minutes=jitter_min)
    end   = base + timedelta(minutes=jitter_min)
    if ref > end:
        base += timedelta(days=1); start = base - timedelta(minutes=jitter_min); end = base + timedelta(minutes=jitter_min)
    start = max(start, ref + timedelta(seconds=5))
    secs = max(60, int((end - start).total_seconds()))
    offs = random.randint(0, secs)
    return start + timedelta(seconds=offs)

def run_daily_at(time_str, jitter_min):
    user_id, token, sheet_url, sheet_tab=load_env()
    print(f"[scheduler] daily at {time_str} ±{jitter_min}min")
    while True:
        nxt = _next_at_with_jitter(time_str, jitter_min)
        print(f"[scheduler] next run at {nxt.isoformat()}")
        while True:
            now = datetime.now(TZ)
            if now >= nxt: break
            time.sleep(min(60, max(1,int((nxt-now).total_seconds()))))
        ws=gs_open(sheet_url, sheet_tab)
        header=ensure_header(ws)
        post_next_unposted(user_id, token, ws, header)
        # 実行した枠は翌日へ
        nxt = _next_at_with_jitter(time_str, jitter_min, ref=nxt + timedelta(days=1))

def run_daily_multi_at(times_csv, jitter_min):
    user_id, token, sheet_url, sheet_tab=load_env()
    times = [t.strip() for t in (times_csv or "").split(",") if t.strip()]
    if not times:
        print("Please set --times as comma-separated HH:MM (JST)", file=sys.stderr); sys.exit(2)
    print(f"[scheduler] daily multi at {times} ±{jitter_min}min")
    # 初期スケジュール
    schedule_map = {t: _next_at_with_jitter(t, jitter_min) for t in times}
    while True:
        # 最も近い予定を実行
        next_t, next_at = min(schedule_map.items(), key=lambda kv: kv[1])
        print(f"[scheduler] next run [{next_t}] at {next_at.isoformat()}")
        while True:
            now = datetime.now(TZ)
            if now >= next_at: break
            time.sleep(min(60, max(1,int((next_at-now).total_seconds()))))
        ws=gs_open(sheet_url, sheet_tab)
        header=ensure_header(ws)
        post_next_unposted(user_id, token, ws, header)
        # 実行済みの時刻は翌日分を再計算
        schedule_map[next_t] = _next_at_with_jitter(next_t, jitter_min, ref=next_at + timedelta(days=1))

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode", choices=["batch","schedule","daily_window","daily_at","daily_multi_at"], default="batch")
    p.add_argument("--interval-min", type=int, default=120)
    p.add_argument("--max-per-run", type=int, default=0)
    p.add_argument("--window", help="HH:MM-HH:MM (JST)", default=None)
    p.add_argument("--time", help="HH:MM (JST)", default=None)
    p.add_argument("--times", help="Comma-separated HH:MM list (JST)", default=None)
    p.add_argument("--jitter-min", type=int, default=30)
    args=p.parse_args()
    if args.mode=="batch":
        run_batch(max_per_run=args.max_per_run)
    elif args.mode=="schedule":
        run_schedule(interval_min=args.interval_min)
    elif args.mode=="daily_window":
        if not args.window: print("Please set --window HH:MM-HH:MM", file=sys.stderr); sys.exit(2)
        run_daily_window(args.window)
    elif args.mode=="daily_at":
        if not args.time: print("Please set --time HH:MM", file=sys.stderr); sys.exit(2)
        run_daily_at(args.time, args.jitter_min)
    elif args.mode=="daily_multi_at":
        if not args.times: print("Please set --times HH:MM,HH:MM,...", file=sys.stderr); sys.exit(2)
        run_daily_multi_at(args.times, args.jitter_min)

if __name__=="__main__":
    # gspread は ~/.config/gspread/service_account.json か環境変数で鍵を参照
    main()
