"""
collect.py - Gemini でコンテンツ生成 → ネタ帳(dedup)保存 + 投稿タブ書き込み

使い方:
  python collect.py --platform threads
  python collect.py --platform x
  python collect.py --platform threads --check-only   # 件数確認のみ
"""
import os, sys, json, time, random, csv, io, re, argparse, base64, tempfile
from datetime import datetime, timezone, timedelta

import requests
import gspread

JST = timezone(timedelta(hours=9))

# 投稿タブのヘッダー（既存スクリプトと共通）
POST_HEADERS = [
    "text", "image_url", "alt_text", "link_attachment",
    "reply_control", "topic_tag", "location_id",
    "status", "posted_at", "error",
]

# ネタ帳(dedup)タブのヘッダー（スプシの列順に合わせる）
DEDUP_HEADERS = [
    "id", "created_at", "platform", "genre",
    "source_account", "source_url", "source_id",
    "original_text", "media_urls", "media_alt_text",
    "keywords", "tone_notes", "rewrite_general",
    "compose_x", "compose_threads",
    "compose_note_title", "compose_note_body_md",
    "hashtags", "mentions", "ocr_text", "ocr_json",
    "status", "scheduled_at", "posted_url", "reviewer", "notes",
    "reviewed",
]
# source_id は DEDUP_HEADERS の7番目（1-indexed = 7）
DEDUP_SOURCE_ID_COL = 7

# Gemini TSV の投稿文列
PLATFORM_TEXT_COL = {
    "threads": "compose_threads",
    "x":       "compose_x",
}


# ── 設定読み込み ──────────────────────────────────────────────

def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_prompt(path="prompts/generate.md"):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── Google Sheets 接続 ────────────────────────────────────────

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

    return gspread.service_account()  # ローカル開発用


def open_sheets(config):
    """投稿タブ と ネタ帳(dedup)タブ を返す"""
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


# ── 件数チェック・重複管理 ────────────────────────────────────

def count_pending(ws):
    """投稿待ち件数を返す（status が posted / done / 済 以外）"""
    records = ws.get_all_records(default_blank="")
    return sum(
        1 for r in records
        if (r.get("text") or r.get("image_url"))
        and str(r.get("status", "")).strip().lower()
            not in ("posted", "done", "済", "posted✅")
    )

def get_used_ids(dedup_ws):
    """ネタ帳の source_id 列（7列目）から使用済みID一覧を返す"""
    vals = dedup_ws.col_values(DEDUP_SOURCE_ID_COL)
    # 1行目はヘッダー（"source_id"）なのでスキップ
    return set(v.strip() for v in vals[1:] if v.strip())


# ── ジャンル抽選 ──────────────────────────────────────────────

def select_genre(genres):
    total = sum(g["weight"] for g in genres)
    r = random.uniform(0, total)
    cum = 0
    for g in genres:
        cum += g["weight"]
        if r <= cum:
            return g["name"]
    return genres[-1]["name"]


# ── Gemini 呼び出し ───────────────────────────────────────────

def build_prompt(template, genre, count):
    return template.replace("{GENRE}", genre).replace("{COUNT}", str(count))

def call_gemini(api_key, prompt_text):
    """Gemini REST API を直接呼び出す。v1/v1beta・複数モデルを自動フォールバック"""
    # (モデル名, APIバージョン) の順番で試す
    candidates = [
        ("gemini-2.0-flash-lite",   "v1beta"),
        ("gemini-2.0-flash",        "v1beta"),
        ("gemini-2.0-flash-exp",    "v1beta"),
    ]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192},
    }
    last_error = ""

    for model_name, api_ver in candidates:
        url = (
            f"https://generativelanguage.googleapis.com/{api_ver}/models"
            f"/{model_name}:generateContent"
        )
        for attempt in range(2):
            try:
                resp = requests.post(
                    url, headers=headers,
                    params={"key": api_key},
                    json=payload, timeout=120,
                )
                if resp.status_code == 200:
                    print(f"[OK] モデル使用: {model_name} ({api_ver})", flush=True)
                    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                elif resp.status_code == 429:
                    wait = 70 * (attempt + 1)
                    print(f"[WARN] 429 ({model_name}/{api_ver}) {attempt+1}/2, {wait}s待機", flush=True)
                    time.sleep(wait)
                    last_error = f"429 rate limit: {model_name}"
                elif resp.status_code == 404:
                    last_error = f"404 not found: {model_name}/{api_ver}"
                    break  # このモデルはスキップ
                else:
                    last_error = f"{resp.status_code}: {resp.text[:200]}"
                    break
            except Exception as e:
                last_error = str(e)
                break

    raise RuntimeError(f"Gemini: 全モデルで失敗しました (最後のエラー: {last_error})")

def parse_tsv(raw_text):
    """コードブロック内の TSV を抽出してパース"""
    m = re.search(r"```(?:text|tsv)?\n(.*?)```", raw_text, re.DOTALL)
    tsv_str = m.group(1) if m else raw_text
    reader = csv.DictReader(io.StringIO(tsv_str.strip()), delimiter="\t")
    return list(reader)


# ── スプシ書き込み ────────────────────────────────────────────

def append_rows(post_ws, dedup_ws, tsv_rows, platform, used_ids, config):
    """
    1. ネタ帳(dedup)に全列データを保存
    2. 投稿タブにテキスト（+ハッシュタグ）を書き込み
    """
    text_col  = PLATFORM_TEXT_COL[platform]
    max_tags  = config.get("max_hashtags", 3)
    now_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    added     = 0

    for row in tsv_rows:
        sid = row.get("source_id", "").strip()
        if not sid or sid in used_ids:
            continue

        text = row.get(text_col, "").strip()
        if not text:
            continue

        # ── ネタ帳に全データ保存 ──
        dedup_row = [
            sid,                                    # id（source_idを流用）
            row.get("created_at", now_str),         # created_at
            row.get("platform", ""),                # platform
            row.get("genre", ""),                   # genre
            row.get("source_account", ""),          # source_account
            row.get("source_url", ""),              # source_url
            sid,                                    # source_id
            row.get("original_text", ""),           # original_text
            row.get("media_urls", ""),              # media_urls
            row.get("media_alt_text", ""),          # media_alt_text
            row.get("keywords", ""),                # keywords
            row.get("tone_notes", ""),              # tone_notes
            row.get("rewrite_general", ""),         # rewrite_general
            row.get("compose_x", ""),               # compose_x
            row.get("compose_threads", ""),         # compose_threads
            row.get("compose_note_title", ""),      # compose_note_title
            row.get("compose_note_body_md", ""),    # compose_note_body_md
            row.get("hashtags", ""),                # hashtags
            "",                                     # mentions（空固定）
            row.get("ocr_text", ""),                # ocr_text
            row.get("ocr_json", ""),                # ocr_json
            "DRAFT",                                # status
            "",                                     # scheduled_at
            "",                                     # posted_url
            "",                                     # reviewer
            row.get("notes", ""),                   # notes
            "",                                     # reviewed
        ]
        dedup_ws.append_row(dedup_row, value_input_option="RAW")
        used_ids.add(sid)
        time.sleep(0.5)

        # ── 投稿タブにテキスト書き込み ──
        raw_tags = row.get("hashtags", "").strip()
        if raw_tags:
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()][:max_tags]
            if tags:
                text = text + "\n\n" + " ".join(tags)

        alt_text  = row.get("media_alt_text", "").strip()
        post_row  = [text, "", alt_text, "", "", "", "", "", "", ""]
        post_ws.append_row(post_row, value_input_option="RAW")
        added += 1
        time.sleep(0.5)

    return added


# ── Discord 通知 ──────────────────────────────────────────────

def notify_discord(webhook_url, title, description, is_error=False):
    if not webhook_url:
        return
    color   = 0xFF0000 if is_error else 0x00CC44
    payload = {"embeds": [{"title": title, "description": description, "color": color}]}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


# ── メイン ────────────────────────────────────────────────────

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

    # 閾値: posts_per_day × threshold_days
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

    template      = load_prompt()
    genres        = config.get("genres", [{"name": "ライバー", "weight": 100}])
    posts_per_run = config.get("posts_per_run", 50)
    batch_size    = 10
    batches       = (posts_per_run + batch_size - 1) // batch_size
    used_ids      = get_used_ids(dedup_ws)
    total_added   = 0

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
            notify_discord(discord_url, "❌ 収集エラー",
                           f"{account_name}/{args.platform}: {msg}", is_error=True)

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
