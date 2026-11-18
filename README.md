# 🛒 AI Supermarket Flyer Notifier (Supermarket Flyer Bot)

好きなスーパーのWebチラシURLを対話形式で登録するだけで、毎朝AI（Gemini Vision）がチラシを読み取り、**「本日有効な特売商品」**だけを抽出して **LINE / Discord / Slack** へ自動通知するサーバーレス・ボットです。

## ✨ 主な特徴

- **AIによるチラシ位置の自動キャリブレーション (`setup_store.py`)**
  - URLを入力すると、Geminiがスクリーンショットから「チラシ紙面の切り抜き座標」と「裏面へめくるボタン位置」を自動推論します。
  - 赤枠付きのプレビュー画像を確認しながら、対話形式で微調整して `stores.json` に保存できます。
- **日付枠の厳密判定（過去日・未来日の誤抽出を防止）**
  - チラシ内の日付ボックス構造を認識し、さらにPython側で期間判定を行うため、前日の特売品などが混入しません。
- **マルチアウトプット対応 (`notifier.py`)**
  - GitHub Secrets に設定した通知先（**LINE Messaging API / Discord Webhook / Slack Webhook**）を自動検知し、有効なチャネルへ一斉配信します。
- **完全サーバーレス運用**
  - GitHub Actions の無料枠のみで毎日自動実行できます。

---

## 🚀 セットアップ手順

### 1. リポジトリのフォーク＆クローン
このリポジトリをフォークし、手元のPCにクローンします。

```bash
git clone https://github.com/<your-username>/supermarket-flyer-bot.git
cd supermarket-flyer-bot
pip install -r requirements.txt
playwright install chromium
```

### 2. 対話スクリプトでスーパーのチラシを登録 (`setup_store.py`)
環境変数に `GEMINI_API_KEY` をセットして、`setup_store.py` を実行します。

```bash
export GEMINI_API_KEY="AIzaSy..."
python setup_store.py
```

1. 店舗名とチラシページのURLを入力します。
2. `setup_previews/` フォルダに、AIが認識した領域（赤枠）と切り抜きプレビュー画像が出力されます。
3. 画像を確認し、問題なければ `y` を入力すると `stores.json` に保存されます。ズレている場合は「もう少し右に」「上部のタイトルを除外して」のように修正指示を送るとAIが再計算します。
4. 登録が終わったら `stores.json` を GitHub にコミット＆プッシュします。

### 3. 抽出商品の好みを調整 (`preferences.py`)
`preferences.py` を編集することで、優先して抽出するカテゴリ（生鮮・卵・牛乳など）や除外カテゴリ（お酒・衣料品など）、1店舗あたりの最大品数を自由に変更できます。

### 4. GitHub Secrets の設定
GitHub リポジトリの **Settings > Secrets and variables > Actions** から、以下のシークレットを登録します。

| Secret名 | 必須 | 説明 |
| :--- | :---: | :--- |
| `GEMINI_API_KEY` | **必須** | Google AI Studio で発行した Gemini API キー |
| `LINE_CHANNEL_ACCESS_TOKEN` | 任意 | LINE Messaging API のチャネルアクセストークン |
| `DISCORD_WEBHOOK_URL` | 任意 | Discord チャンネルの Incoming Webhook URL |
| `SLACK_WEBHOOK_URL` | 任意 | Slack チャンネルの Incoming Webhook URL |

※通知先（LINE / Discord / Slack）は、少なくとも1つ設定されていれば動作します（複数設定するとすべてに同時配信されます）。

### 5. 動作確認
GitHub の **Actions** タブから **Daily Supermarket Flyer Bot** を選択し、**Run workflow** をクリックして通知が届けば完了です！
