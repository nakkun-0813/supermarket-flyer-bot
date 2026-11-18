import os
import requests


def send_notifications(text: str):
    """
    環境変数に設定されている通知先（LINE / Discord / Slack）を自動検知し、
    有効なすべてのチャネルへ特売情報を配信します。
    """
    sent_any = False

    # 1. LINE Messaging API (友だち登録者全員へブロードキャスト)
    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if line_token:
        try:
            res = requests.post(
                "https://api.line.me/v2/bot/message/broadcast",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {line_token}",
                },
                json={"messages": [{"type": "text", "text": text[:4800]}]},
                timeout=15,
            )
            if res.status_code == 200:
                print("[LINE配信] 送信成功", flush=True)
            else:
                print(f"[LINE配信エラー] {res.status_code}: {res.text}", flush=True)
            sent_any = True
        except Exception as e:
            print(f"[LINE配信例外] {e}", flush=True)

    # 2. Discord Incoming Webhook
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_url:
        try:
            # Discordの1メッセージ2000文字制限に合わせて分割送信
            chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
            for idx, chunk in enumerate(chunks, 1):
                res = requests.post(discord_url, json={"content": chunk}, timeout=15)
                print(f"[Discord配信 ({idx}/{len(chunks)})] status={res.status_code}", flush=True)
            sent_any = True
        except Exception as e:
            print(f"[Discord配信例外] {e}", flush=True)

    # 3. Slack Incoming Webhook
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            res = requests.post(slack_url, json={"text": text}, timeout=15)
            print(f"[Slack配信] status={res.status_code}", flush=True)
            sent_any = True
        except Exception as e:
            print(f"[Slack配信例外] {e}", flush=True)

    if not sent_any:
        print(
            "[警告] 通知先の環境変数（LINE_CHANNEL_ACCESS_TOKEN / DISCORD_WEBHOOK_URL / SLACK_WEBHOOK_URL）"
            "が1つも設定されていません。",
            flush=True,
        )
