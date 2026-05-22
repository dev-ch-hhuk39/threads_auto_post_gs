"""
llm_client.py - LLM呼び出しモジュール
  call_gemini()    : Gemini REST API（クラウド）
  call_local_llm() : Ollama（ローカル開発用）
  call_llm()       : LLM_PROVIDER 環境変数で切り替え（デフォルト: gemini）
"""
import os, time, requests

GEMINI_CANDIDATES_DEFAULT = [
    ("gemini-2.5-flash-lite", "v1beta"),
    ("gemini-2.5-flash",      "v1beta"),
    ("gemini-2.5-pro",        "v1beta"),
]


def call_gemini(api_key, prompt_text, temperature=0.9, max_tokens=8192):
    """Gemini REST API を直接呼び出す。複数モデルを自動フォールバック"""
    override = os.environ.get("GEMINI_MODEL_CANDIDATES", "").strip()
    if override:
        candidates = []
        for item in override.split(","):
            item = item.strip()
            if not item:
                continue
            if "@" in item:
                model_name, api_ver = item.split("@", 1)
            else:
                model_name, api_ver = item, "v1beta"
            candidates.append((model_name.strip(), api_ver.strip()))
    else:
        candidates = GEMINI_CANDIDATES_DEFAULT

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
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
                    print(f"[WARN] 429 ({model_name}/{api_ver}), {wait}s待機", flush=True)
                    time.sleep(wait)
                    last_error = f"429 rate limit: {model_name}"
                elif resp.status_code == 404:
                    last_error = f"404 not found: {model_name}/{api_ver}"
                    break
                else:
                    last_error = f"{resp.status_code}: {resp.text[:200]}"
                    break
            except Exception as e:
                last_error = str(e)
                break

    raise RuntimeError(f"Gemini: 全モデルで失敗しました (最後のエラー: {last_error})")


def call_local_llm(prompt_text, model="gemma4:26b"):
    """Ollama ローカル LLM を呼び出す（ローカル開発用）"""
    base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    url = f"{base_url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt_text,
        "stream": False,
        "options": {
            "temperature": 0.9,
            "num_predict": 8192,
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=300)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Ollama が起動していません。`ollama serve` を実行してください")
    if resp.status_code == 200:
        result = resp.json().get("response", "")
        print(f"[OK] モデル使用: {model} (Ollama)", flush=True)
        return result
    raise RuntimeError(f"Ollama エラー: {resp.status_code}: {resp.text[:200]}")


def call_llm(prompt_text):
    """
    LLM呼び出しのエントリーポイント。
    LLM_PROVIDER=ollama の場合は Ollama を使用（ローカル開発用）。
    それ以外（デフォルト）は Gemini API を使用。
    """
    provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "gemma4:26b")
        return call_local_llm(prompt_text, model=model)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が未設定です")
    return call_gemini(api_key, prompt_text)
