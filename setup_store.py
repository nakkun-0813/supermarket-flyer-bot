import os
import json
import time
import hashlib
from io import BytesIO
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

CONFIG_PATH = os.environ.get("STORES_CONFIG_PATH", "stores.json")
PREVIEW_DIR = "setup_previews"
os.makedirs(PREVIEW_DIR, exist_ok=True)


class FlyerLayoutAnalysis(BaseModel):
    needs_initial_click: bool = Field(
        description=(
            "現在の画面が『小さなチラシサムネイルの一覧ページ（下に更新日などが並んでいる状態）』であり、"
            "まず最新のチラシ（通常は一番左または上の最新日付のチラシ）をクリックして拡大ビューアを開く必要がある場合は True。"
            "すでに大きなチラシ紙面（ビューア）が開いている場合は False。"
        )
    )
    initial_click_point: list[int] | None = Field(
        default=None,
        description=(
            "needs_initial_click が True の場合、クリックして開くべき最新チラシの中心座標 [y, x]（0〜1000の正規化座標）。"
            "False の場合は null。"
        )
    )
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
        description="なぜこの領域・操作と判断したかの簡潔な説明"
    )


def get_vision_models(client: genai.Client) -> list[str]:
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
    for offset in range(6):
        draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset], outline=(255, 0, 0))
    if next_pt and len(next_pt) == 2:
        ny = int(next_pt[0] / 1000 * full_img.height)
        nx = int(next_pt[1] / 1000 * full_img.width)
        r = 18
        draw.ellipse([nx - r, ny - r, nx + r, ny + r], outline=(0, 100, 255), width=5)
    overlay.save(save_path, format="JPEG", quality=90)


def analyze_layout(
    client: genai.Client,
    models: list[str],
    full_jpeg_bytes: bytes,
    previous_result: dict | None = None,
    user_feedback: str | None = None,
    already_opened_viewer: bool = False,
) -> FlyerLayoutAnalysis:
    base_prompt = (
        "このWebページのスクリーンショットから、スーパーの『特売チラシ』の取得方法を判定してください。\n"
        "【重要ルール】\n"
        "1. もし画面上に『小さなチラシのサムネイルカードが横に複数並んでいる（下に〇〇月〇〇日更新などと書かれている）』状態であれば、"
        "それは一覧画面です。needs_initial_click=True とし、一番新しいチラシ（通常は一番左のカード）の中心座標を initial_click_point に指定してください。\n"
        "2. すでに大きなチラシ本体（ビューア）が表示されている場合は needs_initial_click=False とし、"
        "サイト上部のヘッダー、店舗名タイトル、右サイドバーの他店広告を除外して、チラシ紙面だけを flyer_box で囲んでください。\n"
        "3. next_page_point は『チラシをめくる』『次へ』『＞』など、現在のチラシを裏面へ切り替えるUIボタンの中心座標のみを指定してください。"
    )

    if already_opened_viewer:
        base_prompt += "\n※すでにサムネイルをクリックしてチラシ詳細ビューアを開いた後の画面です。needs_initial_click は必ず False にして、大きく表示されているチラシ本体を flyer_box で囲んでください。"

    if previous_result and user_feedback:
        base_prompt += (
            f"\n\n【前回推論結果の修正依頼】\n"
            f"前回の推論結果: {json.dumps(previous_result, ensure_ascii=False)}\n"
            f"ユーザーからの修正指示: 「{user_feedback}」\n"
            f"上記の修正指示を反映して、座標や設定を再計算してください。"
        )

    part = types.Part.from_bytes(data=full_jpeg_bytes, mime_type="image/jpeg")

    last_error = None
    for model_name in models:
        for attempt in range(2):
            try:
                print(f"  -> {model_name} で推論中...", flush=True)
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
            except Exception as e:
                last_error = e
                err_msg = str(e)
                if any(code in err_msg for code in ["503", "504", "429", "UNAVAILABLE", "DEADLINE_EXCEEDED"]):
                    print(f"  [一時混雑/タイムアウト] {model_name} が遅延しています。3秒後に再試行/別モデルへ切り替えます...", flush=True)
                    time.sleep(3)
                else:
                    raise

    raise RuntimeError(f"すべてのモデルで推論に失敗しました: {last_error}")


def get_ai_jpeg(page) -> tuple[Image.Image, bytes]:
    full_png = page.screenshot(scale="css")
    full_img = Image.open(BytesIO(full_png))
    ai_img = full_img.convert("RGB").resize((1000, int(1000 * full_img.height / full_img.width)), Image.Resampling.LANCZOS)
    buf = BytesIO()
    ai_img.save(buf, format="JPEG", quality=80)
    return full_img, buf.getvalue()


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        print("エラー: 環境変数 GEMINI_API_KEY が設定されていません。")
        return

    client = genai.Client(http_options=types.HttpOptions(timeout=120000))
    models = get_vision_models(client)
    print(f"保存先設定ファイル: {CONFIG_PATH}")

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

        full_img, full_jpeg = get_ai_jpeg(page)

        prev_dict = None
        feedback = None
        attempt = 1
        initial_click_xy = None

        while True:
            print(f"\n[AI解析 第{attempt}回] チラシ位置を推論しています...")
            analysis = analyze_layout(
                client, models, full_jpeg, prev_dict, feedback,
                already_opened_viewer=(initial_click_xy is not None)
            )

            # 一覧ページと判定された場合は、まずサムネイルをクリックして詳細ビューアを開く
            if analysis.needs_initial_click and analysis.initial_click_point and len(analysis.initial_click_point) == 2 and initial_click_xy is None:
                iy = int(analysis.initial_click_point[0] / 1000 * viewport_h)
                ix = int(analysis.initial_click_point[1] / 1000 * viewport_w)
                initial_click_xy = {"x": ix, "y": iy}
                print(f"  [一覧ページ検出] 最新のチラシサムネイル ({ix}, {iy}) をクリックして拡大画面を開きます...")
                page.mouse.click(ix, iy)
                page.wait_for_timeout(4500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(1000)
                full_img, full_jpeg = get_ai_jpeg(page)
                analysis = analyze_layout(client, models, full_jpeg, None, None, already_opened_viewer=True)

            clip = norm_to_pixel_box(analysis.flyer_box, viewport_w, viewport_h)

            overlay_path = os.path.join(PREVIEW_DIR, f"preview_overlay_{attempt}.jpg")
            crop_front_path = os.path.join(PREVIEW_DIR, f"preview_crop_front_{attempt}.jpg")
            crop_back_path = os.path.join(PREVIEW_DIR, f"preview_crop_back_{attempt}.jpg")

            draw_debug_overlay(full_img, clip, analysis.next_page_point, overlay_path)

            front_bytes = page.screenshot(clip=clip)
            with open(crop_front_path, "wb") as f:
                f.write(front_bytes)

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
                # 初期状態へ戻す
                page.goto(store_url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(3000)
                if initial_click_xy:
                    page.mouse.click(initial_click_xy["x"], initial_click_xy["y"])
                    page.wait_for_timeout(4000)

            print("\n--- AIの認識結果 ---")
            print(f"判断理由: {analysis.reasoning}")
            print(f"事前クリック（一覧→詳細）: {initial_click_xy if initial_click_xy else 'なし'}")
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
                    "initial_click": initial_click_xy,
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