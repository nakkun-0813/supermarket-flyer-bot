import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

from preferences import EXTRACTION_PREFERENCES
from notifier import send_notifications

CONFIG_PATH = "stores.json"
DEBUG_DIR = "debug_images"
os.makedirs(DEBUG_DIR, exist_ok=True)

# 日本時間の今日の日付を取得
JST = timezone(timedelta(hours=9))
NOW_JST = datetime.now(JST)
TODAY_MONTH = NOW_JST.month
TODAY_DAY = NOW_JST.day
TODAY_STR = f"{TODAY_MONTH}/{TODAY_DAY}"


class FlyerItem(BaseModel):
    box_position: str = Field(
        description="その商品が配置されている枠の位置と枠線の特徴（例: '右下の青枠内', '中央の特集枠'）。"
    )
    box_date_label: str = Field(
        description="その商品を囲んでいる枠の日付見出しをそのまま正確に転記（例: '11/18', '11/17〜20', '毎日この価格'）。"
    )
    box_date_type: str = Field(
        description=(
            f"その枠が本日({TODAY_STR})単日の枠なら 'TODAY_ONLY'、"
            f"本日を含む複数日通し期間や常時特売枠なら 'PERIOD_VALID'、"
            f"本日以外の単日枠（過去日・未来日）なら 'OTHER_DATE'。"
        )
    )
    name: str = Field(
        description="商品名と単位（例: '赤たまご 10個入', 'きゅうり 1本'）。同じ商品区画内の文字のみを読むこと。"
    )
    price_without_tax: int | None = Field(
        default=None,
        description="税抜価格（整数）。記載がない・読み取れない場合は null",
    )
    price_with_tax: int | None = Field(
        default=None,
        description="税込価格（整数）。記載がない・読み取れない場合は null",
    )
    condition: str | None = Field(
        default=None,
        description="お一人様1点限り、3割引などの条件（なければ null）",
    )


class FlyerExtractionResult(BaseModel):
    items: list[FlyerItem]


def to_optimized_jpeg_part(img: Image.Image, save_name: str, max_width: int = 2400) -> types.Part:
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)

    save_path = os.path.join(DEBUG_DIR, f"{save_name}.jpg")
    img.save(save_path, format="JPEG", quality=95, subsampling=0, optimize=True)

    with open(save_path, "rb") as f:
        jpeg_bytes = f.read()

    print(f"    -> 画像保存: {save_path} ({img.width}x{img.height}px, {len(jpeg_bytes) // 1024} KB)", flush=True)
    return types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")


def split_spread_image(raw_bytes: bytes, base_name: str) -> list[types.Part]:
    img = Image.open(BytesIO(raw_bytes))
    w, h = img.size
    mid = w // 2
    overlap = int(w * 0.03)
    left_img = img.crop((0, 0, mid + overlap, h))
    right_img = img.crop((mid - overlap, 0, w, h))
    return [
        to_optimized_jpeg_part(left_img, f"{base_name}_L"),
        to_optimized_jpeg_part(right_img, f"{base_name}_R"),
    ]


def capture_store_flyer(page, store: dict) -> list[types.Part]:
    parts = []
    name = store["name"]
    url = store["url"]
    clip = store["clip_area"]
    is_spread = store.get("is_double_spread", False)
    next_click = store.get("next_page_click")

    print(f"[{name}] 読み込み中: {url}", flush=True)
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)

        front_bytes = page.screenshot(clip=clip)
        if is_spread:
            parts.extend(split_spread_image(front_bytes, f"{store['id']}_front"))
        else:
            parts.append(to_optimized_jpeg_part(Image.open(BytesIO(front_bytes)), f"{store['id']}_front"))

        if next_click and "x" in next_click and "y" in next_click:
            page.mouse.click(next_click["x"], next_click["y"])
            page.wait_for_timeout(3500)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(1000)
            back_bytes = page.screenshot(clip=clip)

            if hashlib.md5(front_bytes).hexdigest() != hashlib.md5(back_bytes).hexdigest():
                print(f"  [{name}] 裏面を検出しました", flush=True)
                if is_spread:
                    parts.extend(split_spread_image(back_bytes, f"{store['id']}_back"))
                else:
                    parts.append(to_optimized_jpeg_part(Image.open(BytesIO(back_bytes)), f"{store['id']}_back"))

    except Exception as e:
        print(f"  [キャプチャエラー] {name}: {e}", flush=True)

    return parts


def get_target_models(client: genai.Client) -> list[str]:
    preferred = [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
        "gemini-2.5-pro",
    ]
    try:
        available = [m.name.replace("models/", "") for m in client.models.list()]
        selected = [p for p in preferred if p in available]
        return selected if selected else preferred
    except Exception:
        return preferred


def is_valid_for_today(item: dict) -> bool:
    b_type = str(item.get("box_date_type", "")).upper()
    label = str(item.get("box_date_label", ""))

    if b_type == "OTHER_DATE":
        return False

    range_match = re.search(r"(\d{1,2})/(\d{1,2})\D*[〜～\-ー・▸▶]\D*(?:(\d{1,2})/)?(\d{1,2})", label)
    if range_match:
        m1, d1 = int(range_match.group(1)), int(range_match.group(2))
        m2 = int(range_match.group(3)) if range_match.group(3) else m1
        d2 = int(range_match.group(4))
        return (m1, d1) <= (TODAY_MONTH, TODAY_DAY) <= (m2, d2)

    date_matches = re.findall(r"(\d{1,2})/(\d{1,2})", label)
    if date_matches:
        for m_str, d_str in date_matches:
            if int(m_str) == TODAY_MONTH and int(d_str) == TODAY_DAY:
                return True
        return False

    return True


def build_extraction_prompt(store_name: str) -> str:
    prefs = EXTRACTION_PREFERENCES
    priority_str = "、".join(prefs.get("priority_categories", [])) or "指定なし"
    exclude_str = "、".join(prefs.get("exclude_categories", [])) or "なし"
    custom_inst = prefs.get("custom_instruction", "")

    return (
        f"これらは「{store_name}」のスーパー特売チラシ画像です。本日の日付は「{TODAY_STR}」です。\n\n"
        "【最重要ルール：枠線（ボックス）の境界を絶対に見誤らないこと】\n"
        "1. 日別に枠線で区切られている場合、必ずその商品を直接囲んでいる枠線の見出し日付を `box_date_label` に記入してください。\n"
        f"2. 無理に品数を増やすために、隣の枠（過去日や未来日の枠）にある商品を「{TODAY_STR}」だと偽って出力することは厳禁です。\n"
        f"3. 優先順位として、まず本日「{TODAY_STR}」の単日枠内の商品を抽出し、次に期間通し（{TODAY_STR}を含む期間）や常時特売枠の商品を追加してください。\n"
        "4. 1つの商品区画（商品写真・商品名・価格がセットになっている最小単位）の中で、商品名と価格を1対1で正確にペアリングしてください。\n\n"
        "【ユーザーの抽出好み設定】\n"
        f"- 優先カテゴリ: {priority_str}\n"
        f"- 除外カテゴリ: {exclude_str}\n"
        f"- 追加方針: {custom_inst}"
    )


def extract_deals(client: genai.Client, models: list[str], store_name: str, image_parts: list[types.Part]) -> list[dict]:
    if not image_parts:
        return []

    prompt = build_extraction_prompt(store_name)
    max_items = EXTRACTION_PREFERENCES.get("max_items_per_store", 8)

    for model_name in models:
        max_attempts = 2 if "lite" in model_name else 1
        for attempt in range(max_attempts):
            try:
                print(f"  [AI解析中] {store_name} ({model_name}, 画像{len(image_parts)}枚)...", flush=True)
                response = client.models.generate_content(
                    model=model_name,
                    contents=[*image_parts, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=FlyerExtractionResult,
                        temperature=0.0,
                    ),
                )
                data = json.loads(response.text)
                raw_items = data.get("items", []) if isinstance(data, dict) else data

                valid_items = [item for item in raw_items if is_valid_for_today(item)]
                valid_items.sort(key=lambda x: 0 if x.get("box_date_type") == "TODAY_ONLY" else 1)

                print(f"  [解析成功] {store_name}: 抽出{len(raw_items)}品 -> 当日有効 {len(valid_items)}品", flush=True)
                return valid_items[:max_items]
            except Exception as e:
                err_msg = str(e)
                print(f"  [APIエラー] {store_name} ({model_name}): {err_msg[:160]}", flush=True)
                if ("503" in err_msg or "UNAVAILABLE" in err_msg) and attempt + 1 < max_attempts:
                    time.sleep(3)
                else:
                    break

    return []


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"エラー: {CONFIG_PATH} が見つかりません。先に `python setup_store.py` を実行してください。")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    stores = config.get("stores", [])
    if not stores:
        print("登録されている店舗がありません。先に `python setup_store.py` を実行して店舗を登録してください。")
        return

    client = genai.Client(http_options=types.HttpOptions(timeout=90000))
    target_models = get_target_models(client)
    message_blocks = [f"🛒 本日({TODAY_STR})のスーパー特売まとめ\n"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1400},
            device_scale_factor=3.5,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for store in stores:
            vp = store.get("viewport", {"width": 1440, "height": 1400})
            page.set_viewport_size(vp)

            parts = capture_store_flyer(page, store)
            items = extract_deals(client, target_models, store["name"], parts)

            block = [f"■ {store['name']}"]
            if items:
                for item in items:
                    name = item.get("name", "特売品")
                    p_no_tax = item.get("price_without_tax")
                    p_tax = item.get("price_with_tax")
                    date_label = item.get("box_date_label")
                    cond_val = item.get("condition")

                    if p_no_tax and p_tax:
                        price_str = f"税抜{p_no_tax}円 (税込{p_tax}円)"
                    elif p_no_tax:
                        price_str = f"税抜{p_no_tax}円"
                    elif p_tax:
                        price_str = f"税込{p_tax}円"
                    else:
                        price_str = "特価"

                    notes = [x for x in [date_label, cond_val] if x and str(x).lower() != "null"]
                    cond_str = f" ※{'・'.join(notes)}" if notes else ""
                    block.append(f"・{name}：{price_str}{cond_str}")
            else:
                block.append("・本日のチラシ情報を取得できませんでした")

            block.append(f"🔗 {store['url']}")
            message_blocks.append("\n".join(block))
            time.sleep(2)

        browser.close()

    final_text = "\n\n".join(message_blocks)
    print("\n--- 送信メッセージプレビュー ---", flush=True)
    print(final_text, flush=True)
    send_notifications(final_text)


if __name__ == "__main__":
    main()
