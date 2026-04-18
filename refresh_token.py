import os, sys, requests

def main():
    current_token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not current_token:
        print("エラー: THREADS_ACCESS_TOKEN が設定されていません。")
        sys.exit(1)

    url = "https://graph.threads.net/refresh_access_token"
    params = {
        "grant_type": "th_refresh_token",
        "access_token": current_token
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        new_token = data.get("access_token")
        
        if not new_token:
            print("エラー: APIからの応答に access_token が含まれていません。")
            sys.exit(1)

        # 取得した新しいトークンをGitHub Actionsの環境変数(GITHUB_OUTPUT)に渡す
        env_file = os.getenv('GITHUB_OUTPUT')
        with open(env_file, "a") as f:
            f.write(f"NEW_TOKEN={new_token}\n")
            
        print("アクセストークンのリフレッシュに成功しました。")

    except requests.exceptions.RequestException as e:
        print(f"APIリクエストエラー: {e}")
        if 'resp' in locals() and resp is not None:
            print(f"レスポンス詳細: {resp.text}")
        sys.exit(1)

if __name__ == "__main__":
    main()
