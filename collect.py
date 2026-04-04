"""
collect.py - Gemini でコンテンツ生成 → スプシ書き込み

使い方:
  python collect.py --platform threads
  python collect.py --platform x
  python collect.py --platform threads --check-only   # 件数確認のみ
"""
import os, sys, json, time, random, csv, io, re, argparse, base64, tempfile
from datetime import datetime, timezone, timedelta

import requests
import gspread
import google.generativeai as genai

JST = timezone(timedelta(hours=9))

# スプシ投稿タブのヘッダー（既存スクリプトと共通）
SHEET_HEADERS = [
    "text", "image_url", "alt_text", "link_attachment",
    "reply_control", "topic_tag", "location_id",
    "status", "posted_at", "error",
]

# Gemini TSV の投稿文列
PLATFORM_TEXT_COL = {
    "threads": "compose_threads",
    "x":       "compose_x",
}


# ── 設定読み込み ─────────────────────────────────────────────

def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_prompt(path="prompts/generate.md"):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── Google Sheets 接続 ───────────────────────────────────────

def _gc_from_env():
    """SA_JSON_BASE64 または GCP_SA_JSON から gspread クライアントを返す"""
    b64 = os.environ.get("SA_JSON_BASE64", "").strip()
    raw = os.environ.get("GCP_SA_JSON", "").strip()

    if b64:
        decoded = base64.b64decode(b64)
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="wb")
        tmp.write(decoded)
        tmp.close()
        gc = gspread.service_account(filename=tmp.name)
        os.unlink(tmp.name)
        return gc

    if raw:
        from google.oauth2.service_account import Credentials
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return gspread.authorize(creds)

    # ローカル開発用: ~/.config/gspread/service_account.json
    return gspread.service_account()


def open_sheets(config):
    """投稿タブ と 重複管理タブ を返す"""
    gc = _gc_from_env()

    sheet_url = os.environ.get("SHEET_URL", "").strip()
    sheet_id  = os.environ.get("SHEET_ID", "").strip()
    sheet_tab = os.environ.get("SHEET_TAB", config.get("sheet_tab", "")).strip()
    dedup_tab = os.environ.get("DEDUP_TAB", config.get("dedup_tab", "dedup")).strip()

    if sheet_url:
        sh = gc.open_by_url(sheet_url)
    elif sheet_id:
        sh = gc.open_by_key(sheet_id)
    else:
        raise RuntimeError("SHEET_URL または SHEET_ID が未設定です")

    post_ws  = sh.worksheet(sheet_tab) if sheet_tab else sh.sheet1
    dedup_ws = sh.worksheet(dedup_tab)
    return post_ws, dedup_ws


# ── 件数チェック・重複管理 ───────────────────────────────────

def count_pending(ws):
    """投稿待ち（status が posted / done / 済 以外）の件数を返す"""
    records = ws.get_all_records(default_blank="")
    return sum(
        1 for r in records
        if (r.get("text") or r.get("image_url"))
        and str(r.get("status", "")).strip().lower()
            not in ("posted", "done", "済", "posted✅")
    )

def get_used_ids(dedup_ws):
    """重複管理タブの source_id 一覧を返す"""
    vals = dedup_ws.col_values(1)
    return set(v.strip() for v in vals if v.strip())


# ── ジャンル抽選 ─────────────────────────────────────────────

def select_genre(genres):
    total = sum(g["weight"] for g in genres)
    r = random.uniform(0, total)
    cum = 0
    for g in genres:
        cum += g["weight"]
        if r <= cum:
            return g["name"]
    return genres[-1]["name"]


# ── Gemini 呼び出し ──────────────────────────────────────────

def build_prompt(template, genre, count):
    p = template.replace("{GENRE}", genre).replace("{COUNT}", str(count))
    return p

def call_gemini(api_key, prompt_text):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    resp = model.generate_content(prompt_text)
    return resp.text

def parse_tsv(raw_text):
    """コードブロック内の TSV を抽出してパース"""
    m = re.search(r"```(?:text|tsv)?\n(.*?)```", raw_text, re.DOTALL)
    tsv_str = m.group(1) if m else raw_text
    reader = csv.DictReader(io.StringIO(tsv_str.strip()), delimiter="\t")
    return list(reader)


# ── スプシ書き込み ───────────────────────────────────────────

def append_rows(post_ws, dedup_ws, tsv_rows, platform, used_ids, config):
    text_col    = PLATFORM_TEXT_COL[platform]
    max_tags    = config.get("max_hashtags", 3)
    added       = 0
    new_ids     = []

    for row in tsv_rows:
        sid = row.get("source_id", "").strip()
        if not sid or sid in used_ids:
            continue

        text = row.get(text_col, "").strip()
        if not text:
            continue

        # ハッシュタグを末尾付与（上位 max_tags 件）
        raw_tags = row.get("hashtags", "").strip()
        if raw_tags:
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()][:max_tags]
            if tags:
                text = text + "\n\n" + " ".join(tags)

        alt_text = row.get("media_alt_text", "").strip()

        sheet_row = [
            text, "", alt_text, "", "", "", "",  # text〜location_id
            "",   "", "",                         # status, posted_at, error
        ]
        post_ws.append_row(sheet_row, value_input_option="RAW")
        used_ids.add(sid)
        new_ids.append(sid)
        added += 1
        time.sleep(0.6)  # Sheets API レートリミット対策

    # 重複管理タブに source_id を記録
    if new_ids:
        now_str = datetime.now(JST).isoformat(timespec="seconds")
        for sid in new_ids:
            dedup_ws.append_row([sid, now_str], value_input_option="RAW")
            time.sleep(0.3)

    return added


# ── Discord 通知 ─────────────────────────────────────────────

def notify_discord(webhook_url, title, description, is_error=False):
    if not webhook_url:
        return
    color   = 0xFF0000 if is_error else 0x00CC44
    payload = {"embeds": [{"title": title, "description": description, "color": color}]}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


# ── メイン ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["threads", "x"], required=True)
    parser.add_argument("--check-only", action="store_true",
                        help="pending件数を確認するだけ（生成しない）")
    args = parser.parse_args()

    config       = load_config()
    gemini_key   = os.environ.get("GEMINI_API_KEY", "").strip()
    discord_url  = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    account_name = config.get("account_name", "")

    post_ws, dedup_ws = open_sheets(config)

    # 閾値計算: posts_per_day × threshold_days
    threshold = config.get("posts_per_day", 8) * config.get("threshold_days", 7)
    pending   = count_pending(post_ws)
    print(f"[INFO] pending={pending}, threshold={threshold}", flush=True)

    if pending >= threshold:
        print(f"[SKIP] コンテンツ充足 ({pending} >= {threshold})", flush=True)
        return

    if args.check_only:
        print(f"[CHECK] {config.get('posts_per_run', 50)} 件生成が必要", flush=True)
        return

    if not gemini_key:
        msg = "GEMINI_API_KEY が未設定です"
        notify_discord(discord_url, "❌ 収集エラー", f"{account_name}: {msg}", is_error=True)
        sys.exit(1)

    template     = load_prompt()
    genres       = config.get("genres", [{"name": "ライバー", "weight": 100}])
    posts_per_run = config.get("posts_per_run", 50)
    batch_size   = 10
    batches      = (posts_per_run + batch_size - 1) // batch_size
    used_ids     = get_used_ids(dedup_ws)
    total_added  = 0

    for i in range(batches):
        genre  = select_genre(genres)
        count  = min(batch_size, posts_per_run - total_added)
        prompt = build_prompt(template, genre, count)

        print(f"[GEN] batch {i+1}/{batches} genre={genre} count={count}", flush=True)
        try:
            raw  = call_gemini(gemini_key, prompt)
            rows = parse_tsv(raw)
            n    = append_rows(post_ws, dedup_ws, rows, args.platform, used_ids, config)
            total_added += n
            print(f"[OK] +{n} 件追加", flush=True)
        except Exception as e:
            msg = f"batch {i+1} エラー: {e}"
            print(f"[ERROR] {msg}", flush=True)
            notify_discord(discord_url, "❌ 収集エラー", f"{account_name}/{args.platform}: {msg}", is_error=True)

        if i < batches - 1:
            time.sleep(4)

    notify_discord(
        discord_url,
        "📥 収集完了",
        f"{account_name}/{args.platform}: {total_added} 件生成・スプシ追記完了",
    )
    print(f"[DONE] 合計 {total_added} 件追加", flush=True)


if __name__ == "__main__":
    main()
