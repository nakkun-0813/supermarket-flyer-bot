import os
import json
import hashlib
from io import BytesIO
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

CONFIG_PATH = "stores.json"
PREVIEW_DIR = "setup_previews"
os.makedirs(PREVIEW_DIR, exist_ok=True)


class FlyerLayoutAnalysis(BaseModel):
    flyer_box: list[int] = Field(
        description=(
            "チラシ本体が描画されている領域のバウンディングボックス [ymin, xmin, ymax, xmax] "
            "（0〜1000の正規化座標）。Webサイトのヘッダー、ナビゲーション、他店広告、余白は除外し、"
            "チラシの紙面そのものだけを正確に囲むこと。"
        )
    )
    is_double_spread: bool = Field(
        description="チラシが見開き2ページ構成（横長で左右に1ページずつ並んでいる）なら True、縦長1ページなら False"
    )
    has_next_page: bool = Field(
        description="画面上に『次へ』『チラシをめくる』『裏面』などのページ送りボタンや矢印が存在すれば True"
    )
    next_page_point: list[int] | None = Field(
        default=None,
        description=(
            "次ページ（裏面）へ切り替えるボタン・矢印の中心座標 [y, x]（0〜1000の正規化座標）。"
            "他店のチラシリンクや広告バナーの座標は絶対に選ばないこと。存在しない場合は null。"
        )
    )
    reasoning: str = Field(
        description="なぜこの領域とボタン位置と判断したかの簡潔な説明"
    )


def get_vision_model(client: genai.Client) -> str:
    preferred = [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-pro",
    ]
    try:
        available = [m.name.replace("models/", "") for m in client.models.list()]
        for p in preferred:
            if p in available:
                return p
    except Exception:
        pass
    return preferred[0]


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"stores": []}


def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def norm_to_pixel_box(norm_box: list[int], width: int, height: int) -> dict:
    ymin, xmin, ymax, xmax = [max(0, min(1000, int(v))) for v in norm_box]
    x = int(xmin / 1000 * width)
    y = int(ymin / 1000 * height)
    w = max(50, int((xmax - xmin) / 1000 * width))
    h = max(50, int((ymax - ymin) / 1000 * height))
    return {"x": x, "y": y, "width": min(w, width - x), "height": min(h, height - y)}


def draw_debug_overlay(full_img: Image.Image, clip: dict, next_pt: list[int] | None, save_path: str):
    overlay = full_img.copy().convert("RGB")
    draw = ImageDraw.Draw(overlay)
    x1, y1 = clip["x"], clip["y"]
    x2, y2 = x1 + clip["width"], y1 + clip["height"]
    # 赤枠でチラシ認識領域を描画
    for offset in range(6):
        draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset], outline=(255, 0, 0))
    # 青丸で次ページボタン位置を描画
    if next_pt and len(next_pt) == 2:
        ny = int(next_pt[0] / 1000 * full_img.height)
        nx = int(next_pt[1] / 1000 * full_img.width)
        r = 18
        draw.ellipse([nx - r, ny - r, nx + r, ny + r], outline=(0, 100, 255), width=5)
    overlay.save(save_path, format="JPEG", quality=90)


def analyze_layout(
    client: genai.Client,
    model_name: str,
    full_jpeg_bytes: bytes,
    previous_result: dict | None = None,
    user_feedback: str | None = None,
) -> FlyerLayoutAnalysis:
    base_prompt = (
        "このWebページのスクリーンショットから、スーパーの『特売チラシ本体』の領域と、"
        "『次ページ（裏面）へめくるボタン』の位置を特定してください。\n"
        "【重要ルール】\n"
        "- サイト上部のヘッダー、店舗名タイトル、検索窓、右サイドバーの他店チラシ広告は絶対に flyer_box に含めないでください。\n"
        "- チラシの紙面（商品写真や価格が載っている長方形領域）だけを正確に囲んでください。\n"
        "- next_page_point は『チラシをめくる』『次へ』『＞』など、現在の店舗のチラシを裏面へ切り替えるUIボタンの中心座標のみを指定してください。"
    )

    if previous_result and user_feedback:
        base_prompt += (
            f"\n\n【前回推論結果の修正依頼】\n"
            f"前回の推論結果: {json.dumps(previous_result, ensure_ascii=False)}\n"
            f"ユーザーからの修正指示: 「{user_feedback}」\n"
            f"上記の修正指示を反映して、座標や設定を再計算してください。"
        )

    part = types.Part.from_bytes(data=full_jpeg_bytes, mime_type="image/jpeg")
    response = client.models.generate_content(
        model=model_name,
        contents=[part, base_prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FlyerLayoutAnalysis,
            temperature=0.0,
        ),
    )
    return FlyerLayoutAnalysis.model_validate_json(response.text)


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        print("エラー: 環境変数 GEMINI_API_KEY が設定されていません。")
        return

    client = genai.Client(http_options=types.HttpOptions(timeout=90000))
    model_name = get_vision_model(client)
    print(f"使用モデル: {model_name}")

    store_name = input("1. 登録する店舗名を入力してください（例: ダイエー池田駅前店）: ").strip()
    store_url = input("2. ターゲットとするスーパーのチラシURLを入力してください: ").strip()
    if not store_name or not store_url:
        print("店舗名とURLは必須です。")
        return

    viewport_w, viewport_h = 1440, 1400

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport_w, "height": viewport_h},
            device_scale_factor=2.0,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        print(f"\nページを読み込んでいます: {store_url} ...")
        page.goto(store_url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)

        # AI解析用に等倍相当の全体スクリーンショットを取得
        full_png = page.screenshot(scale="css")
        full_img = Image.open(BytesIO(full_png))
        buf = BytesIO()
        full_img.convert("RGB").save(buf, format="JPEG", quality=85)
        full_jpeg = buf.getvalue()

        prev_dict = None
        feedback = None
        attempt = 1

        while True:
            print(f"\n[AI解析 第{attempt}回] チラシ位置を推論しています...")
            analysis = analyze_layout(client, model_name, full_jpeg, prev_dict, feedback)
            clip = norm_to_pixel_box(analysis.flyer_box, viewport_w, viewport_h)

            overlay_path = os.path.join(PREVIEW_DIR, f"preview_overlay_{attempt}.jpg")
            crop_front_path = os.path.join(PREVIEW_DIR, f"preview_crop_front_{attempt}.jpg")
            crop_back_path = os.path.join(PREVIEW_DIR, f"preview_crop_back_{attempt}.jpg")

            draw_debug_overlay(full_img, clip, analysis.next_page_point, overlay_path)

            # 表面の切り抜きプレビュー保存
            front_bytes = page.screenshot(clip=clip)
            with open(crop_front_path, "wb") as f:
                f.write(front_bytes)

            # 裏面ボタンがあればテストクリックして裏面プレビューも保存
            has_back_captured = False
            next_click_xy = None
            if analysis.has_next_page and analysis.next_page_point and len(analysis.next_page_point) == 2:
                ny = int(analysis.next_page_point[0] / 1000 * viewport_h)
                nx = int(analysis.next_page_point[1] / 1000 * viewport_w)
                next_click_xy = {"x": nx, "y": ny}
                page.mouse.click(nx, ny)
                page.wait_for_timeout(3500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(1000)
                back_bytes = page.screenshot(clip=clip)
                if hashlib.md5(front_bytes).hexdigest() != hashlib.md5(back_bytes).hexdigest():
                    has_back_captured = True
                    with open(crop_back_path, "wb") as f:
                        f.write(back_bytes)
                # 次回再試行に備えてリロードして初期状態に戻す
                page.goto(store_url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(3000)

            print("\n--- AIの認識結果 ---")
            print(f"判断理由: {analysis.reasoning}")
            print(f"切り抜き座標: {clip}")
            print(f"見開き左右分割: {'あり' if analysis.is_double_spread else 'なし'}")
            print(f"裏面キャプチャテスト: {'成功 (' + crop_back_path + ')' if has_back_captured else 'なし / 未検出'}")
            print(f"\n以下の画像を確認してください:")
            print(f"  1. 全体枠確認（赤枠=チラシ領域 / 青丸=次へボタン）: {overlay_path}")
            print(f"  2. 実際の切り抜き画像（表面）: {crop_front_path}")
            if has_back_captured:
                print(f"  3. 実際の切り抜き画像（裏面）: {crop_back_path}")

            ans = input("\nこの位置でチラシを読み込んでよろしいですか？ (y = 確定保存 / 修正指示を入力 = 再検討): ").strip()
            if ans.lower() in ["y", "yes", "ok", "はい"]:
                config = load_config()
                store_id = f"store_{len(config['stores']) + 1}"
                entry = {
                    "id": store_id,
                    "name": store_name,
                    "url": store_url,
                    "viewport": {"width": viewport_w, "height": viewport_h},
                    "clip_area": clip,
                    "is_double_spread": analysis.is_double_spread,
                    "next_page_click": next_click_xy if has_back_captured else None,
                }
                config["stores"].append(entry)
                save_config(config)
                print(f"\n保存完了！ '{CONFIG_PATH}' に {store_name} を登録しました。")
                break
            else:
                prev_dict = analysis.model_dump()
                feedback = ans if ans else "枠の位置がずれています。チラシの紙面全体が過不足なく収まるように調整してください。"
                attempt += 1

        browser.close()


if __name__ == "__main__":
    main()