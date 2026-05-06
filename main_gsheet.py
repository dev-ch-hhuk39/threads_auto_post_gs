import argparse
import json
import os
import sys
import time

import gspread
import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

API_BASE = "https://graph.threads.net/v1.0"

QUEUE_HEADERS = [
    "キューID",
    "元投稿ID",
    "元投稿URL",
    "アカウント名",
    "投稿文",
    "画像URL",
    "動画URL",
    "ドライブ画像ファイルID",
    "ドライブ動画ファイルID",
    "Threads画像URL",
    "Threads動画URL",
    "採用案",
    "転載可否",
    "確認メモ",
    "X投稿対象",
    "X投稿状態",
    "X投稿日時",
    "Threads投稿対象",
    "Threads投稿状態",
    "Threads投稿日時",
    "キュー追加日時",
    "最終更新日時",
]


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _service_account_path():
    return (
        os.environ.get("GSPREAD_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.path.expanduser("~/.config/gspread/service_account.json")
    )


def load_env():
    load_dotenv()
    user_id = os.getenv("THREADS_USER_ID", "").strip()
    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    sheet_url = os.getenv("SHEET_URL", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    sheet_tab = os.getenv("SHEET_TAB", "03_投稿キュー").strip()
    if not user_id or not token:
        print("THREADS_USER_ID or THREADS_ACCESS_TOKEN missing", file=sys.stderr)
        sys.exit(2)
    if not sheet_url and not sheet_id:
        print("SHEET_URL or SHEET_ID missing", file=sys.stderr)
        sys.exit(2)
    return user_id, token, sheet_url, sheet_id, sheet_tab


def gs_open(sheet_url, sheet_id, sheet_tab):
    sa = _service_account_path()
    gc = gspread.service_account(filename=sa) if os.path.exists(sa) else gspread.service_account()
    sh = gc.open_by_url(sheet_url) if sheet_url else gc.open_by_key(sheet_id)
    return sh.worksheet(sheet_tab) if sheet_tab else sh.sheet1


def ensure_header(ws):
    values = ws.get_all_values()
    if not values:
        ws.update(values=[QUEUE_HEADERS], range_name="1:1")
        return QUEUE_HEADERS
    header = values[0]
    if header != QUEUE_HEADERS:
        ws.update(values=[QUEUE_HEADERS], range_name="1:1")
        return QUEUE_HEADERS
    return header


def rows_with_index(ws, header):
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    rows = []
    for i, raw in enumerate(values[1:], start=2):
        row = {h: (raw[idx] if idx < len(raw) else "") for idx, h in enumerate(header)}
        rows.append((i, row))
    return rows


@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 4), reraise=True)
def create_container(user_id, token, payload):
    url = f"{API_BASE}/{user_id}/threads"
    resp = requests.post(url, headers=auth_headers(token), json=payload, timeout=30)
    if resp.status_code >= 400:
        raise Exception(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 4), reraise=True)
def publish_container(user_id, token, creation_id):
    url = f"{API_BASE}/{user_id}/threads_publish"
    resp = requests.post(url, headers=auth_headers(token), params={"creation_id": creation_id}, timeout=30)
    if resp.status_code >= 400:
        raise Exception(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def wait_for_container_ready(token, creation_id, timeout_seconds=180):
    deadline = time.time() + timeout_seconds
    last_payload = {}
    while time.time() < deadline:
        resp = requests.get(
            f"{API_BASE}/{creation_id}",
            headers=auth_headers(token),
            params={"fields": "status,error_message"},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise Exception(f"HTTP {resp.status_code}: {resp.text}")
        last_payload = resp.json()
        status = str(last_payload.get("status", "")).upper()
        if status in {"FINISHED", "PUBLISHED"}:
            return last_payload
        if status in {"ERROR", "EXPIRED"}:
            raise Exception(f"Threads container failed: {json.dumps(last_payload, ensure_ascii=False)}")
        time.sleep(5)
    raise Exception(f"Threads container was not ready within {timeout_seconds}s: {json.dumps(last_payload, ensure_ascii=False)}")


def post_one(user_id, token, row):
    text = (row.get("投稿文") or "").strip()
    image_url = (row.get("Threads画像URL") or row.get("画像URL") or "").strip()
    video_url = (row.get("Threads動画URL") or row.get("動画URL") or "").strip()

    if video_url:
        payload = {"media_type": "VIDEO", "video_url": video_url, "text": text}
        data = create_container(user_id, token, payload)
        cid = data.get("id")
        wait_for_container_ready(token, cid)
        pub = publish_container(user_id, token, cid)
        return {"status": "published", "container_id": cid, "media_type": "VIDEO", "publish": pub}

    if image_url:
        payload = {"media_type": "IMAGE", "image_url": image_url, "text": text}
        data = create_container(user_id, token, payload)
        cid = data.get("id")
        pub = publish_container(user_id, token, cid)
        return {"status": "published", "container_id": cid, "media_type": "IMAGE", "publish": pub}

    payload = {"media_type": "TEXT", "text": text, "auto_publish_text": True}
    data = create_container(user_id, token, payload)
    return {"status": "published", "container_id": data.get("id"), "media_type": "TEXT"}


def update_cells(ws, row_idx, header, updates):
    col_idx = {h: i + 1 for i, h in enumerate(header)}
    cells = [gspread.Cell(row=row_idx, col=col_idx[key], value=value) for key, value in updates.items() if key in col_idx]
    if cells:
        ws.update_cells(cells, value_input_option="RAW")


def first_pending(rows):
    for i, row in rows:
        target = (row.get("Threads投稿対象") or "").strip()
        status = (row.get("Threads投稿状態") or "").strip()
        text = (row.get("投稿文") or "").strip()
        if target == "投稿する" and status == "投稿待ち" and text:
            return i, row
    return None


def post_next_unposted(user_id, token, ws, header):
    rows = rows_with_index(ws, header)
    pick = first_pending(rows)
    if not pick:
        print(json.dumps({"ok": True, "msg": "no threads rows to post"}, ensure_ascii=False))
        return False

    row_idx, row = pick
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        res = post_one(user_id, token, row)
        update_cells(
            ws,
            row_idx,
            header,
            {
                "Threads投稿状態": "投稿済み",
                "Threads投稿日時": ts,
                "最終更新日時": ts,
            },
        )
        print(json.dumps({"ok": True, "row_idx": row_idx, "row": row, "res": res}, ensure_ascii=False))
        return True
    except Exception as e:
        err = str(e)
        note = (row.get("確認メモ") or "").strip()
        merged_note = f"{note} / Threads投稿エラー: {err}".strip(" /")
        update_cells(
            ws,
            row_idx,
            header,
            {
                "Threads投稿状態": "エラー",
                "確認メモ": merged_note[:3000],
                "最終更新日時": ts,
            },
        )
        print(json.dumps({"ok": False, "row_idx": row_idx, "row": row, "err": err}, ensure_ascii=False))
        return False


def run_batch(max_per_run=0):
    user_id, token, sheet_url, sheet_id, sheet_tab = load_env()
    ws = gs_open(sheet_url, sheet_id, sheet_tab)
    header = ensure_header(ws)
    n = 0
    while True:
        ok = post_next_unposted(user_id, token, ws, header)
        if not ok:
            break
        n += 1
        if max_per_run and n >= max_per_run:
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["batch"], default="batch")
    ap.add_argument("--max-per-run", type=int, default=1)
    args = ap.parse_args()
    run_batch(max_per_run=args.max_per_run)


if __name__ == "__main__":
    main()
