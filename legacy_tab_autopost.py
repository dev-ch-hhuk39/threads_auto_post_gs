import datetime as dt
import json
import os
import sys
import time
from urllib.parse import urlparse

import gspread
import requests
from dotenv import load_dotenv

TZ = dt.timezone(dt.timedelta(hours=9), name="JST")
API_BASE = "https://graph.threads.net/v1.0"

POST_HEADERS = [
    "text",
    "image_url",
    "alt_text",
    "link_attachment",
    "reply_control",
    "topic_tag",
    "location_id",
    "status",
    "posted_at",
    "error",
]

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}


def log(*args):
    print(*args)
    sys.stdout.flush()


def need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"[FATAL] missing env: {name}")
    return value


def auth_headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def _service_account_path():
    return (
        os.environ.get("GSPREAD_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.path.expanduser("~/.config/gspread/service_account.json")
    )


def load_env():
    load_dotenv()
    user_id = need("THREADS_USER_ID")
    token = need("THREADS_ACCESS_TOKEN")
    sheet_url = os.getenv("SHEET_URL", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    sheet_tab = os.getenv("SHEET_TAB", "auto-posttab").strip()
    if not sheet_url and not sheet_id:
        raise RuntimeError("[FATAL] SHEET_URL or SHEET_ID is required")
    return user_id, token, sheet_url, sheet_id, sheet_tab


def gs_open(sheet_url, sheet_id, sheet_tab):
    sa = _service_account_path()
    gc = gspread.service_account(filename=sa) if os.path.exists(sa) else gspread.service_account()
    sh = gc.open_by_url(sheet_url) if sheet_url else gc.open_by_key(sheet_id)
    return sh.worksheet(sheet_tab) if sheet_tab else sh.sheet1


def ensure_header(ws):
    values = ws.get_all_values()
    if not values:
        ws.update(values=[POST_HEADERS], range_name="1:1")
        return POST_HEADERS
    header = values[0]
    if header != POST_HEADERS:
        ws.update(values=[POST_HEADERS], range_name="1:1")
        return POST_HEADERS
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


def is_pending_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    return text not in {"posted", "done", "済", "posted✅"}


def first_pending(rows):
    for i, row in rows:
        text = (row.get("text") or "").strip()
        image_url = (row.get("image_url") or "").strip()
        if (text or image_url) and is_pending_status(row.get("status", "")):
            return i, row
    return None


def looks_like_video(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in VIDEO_EXTENSIONS)


def create_container(user_id: str, token: str, payload: dict):
    url = f"{API_BASE}/{user_id}/threads"
    resp = requests.post(url, headers=auth_headers(token), json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Threads create failed: {resp.status_code} {resp.text}")
    return resp.json()


def publish_container(user_id: str, token: str, creation_id: str):
    url = f"{API_BASE}/{user_id}/threads_publish"
    resp = requests.post(url, headers=auth_headers(token), params={"creation_id": creation_id}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Threads publish failed: {resp.status_code} {resp.text}")
    return resp.json()


def wait_for_container_ready(token: str, creation_id: str, timeout_seconds=180):
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
            raise RuntimeError(f"Threads container check failed: {resp.status_code} {resp.text}")
        last_payload = resp.json()
        status = str(last_payload.get("status", "")).upper()
        if status in {"FINISHED", "PUBLISHED"}:
            return last_payload
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Threads container failed: {json.dumps(last_payload, ensure_ascii=False)}")
        time.sleep(5)
    raise RuntimeError(f"Threads container was not ready within {timeout_seconds}s: {json.dumps(last_payload, ensure_ascii=False)}")


def post_threads(user_id: str, token: str, text: str, media_url: str):
    media_url = (media_url or "").strip()
    if media_url:
        if looks_like_video(media_url):
            payload = {"media_type": "VIDEO", "video_url": media_url, "text": text}
        else:
            payload = {"media_type": "IMAGE", "image_url": media_url, "text": text}
        data = create_container(user_id, token, payload)
        cid = data.get("id")
        if not cid:
            raise RuntimeError(f"Threads create succeeded but id missing: {json.dumps(data, ensure_ascii=False)}")
        if payload["media_type"] == "VIDEO":
            wait_for_container_ready(token, cid)
        publish_container(user_id, token, cid)
        return cid

    payload = {"media_type": "TEXT", "text": text, "auto_publish_text": True}
    data = create_container(user_id, token, payload)
    cid = data.get("id")
    if not cid:
        raise RuntimeError(f"Threads text create succeeded but id missing: {json.dumps(data, ensure_ascii=False)}")
    return cid


def update_cells(ws, row_idx, header, updates):
    col_idx = {h: i + 1 for i, h in enumerate(header)}
    cells = [gspread.Cell(row=row_idx, col=col_idx[key], value=value) for key, value in updates.items() if key in col_idx]
    if cells:
        ws.update_cells(cells, value_input_option="RAW")


def run():
    user_id, token, sheet_url, sheet_id, sheet_tab = load_env()
    ws = gs_open(sheet_url, sheet_id, sheet_tab)
    header = ensure_header(ws)
    rows = rows_with_index(ws, header)
    pick = first_pending(rows)

    if not pick:
        log("[DONE] No pending legacy Threads posts.")
        return

    row_idx, row = pick
    text = (row.get("text") or "").strip()
    media_url = (row.get("image_url") or "").strip()
    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    log(f"[TARGET] Row {row_idx}: {text[:40]}...")

    try:
        container_id = post_threads(user_id, token, text, media_url)
        log(f"[SUCCESS] Threads container ID: {container_id}")
        update_cells(
            ws,
            row_idx,
            header,
            {
                "status": "posted",
                "posted_at": now,
                "error": "",
            },
        )
    except Exception as exc:
        message = str(exc)
        log(f"[FAIL] {message}")
        update_cells(
            ws,
            row_idx,
            header,
            {
                "status": "error",
                "error": message[:3000],
            },
        )
        raise


if __name__ == "__main__":
    run()
