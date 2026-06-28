import os
import json
import base64
import asyncio
import threading
from datetime import datetime
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from playwright.async_api import async_playwright
import anthropic

app = Flask(__name__)
CORS(app)

data_store = {
    "rounds": [],
    "status": "idle",
    "last_scan": None,
    "error": None,
    "scanning": False
}

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

XBET_USERNAME = os.environ.get("XBET_USERNAME")
XBET_PASSWORD = os.environ.get("XBET_PASSWORD")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "15"))

browser_context = None
page_ref = None
loop = None


async def login_and_navigate(playwright):
    global browser_context, page_ref

    print("[BROWSER] Launching Chromium...")
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-web-security",
            "--window-size=1280,800"
        ]
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True
    )

    page = await context.new_page()
    page.set_default_timeout(120000)  # 2 minutes global timeout

    # Step 1: Go directly to login page
    print("[LOGIN] Going to login page...")
    try:
        await page.goto(
            "https://1xbet.com/en/login",
            wait_until="domcontentloaded",
            timeout=120000
        )
    except Exception as e:
        print(f"[LOGIN] Direct login page failed: {e}, trying main page...")
        await page.goto(
            "https://1xbet.com",
            wait_until="domcontentloaded",
            timeout=120000
        )

    await asyncio.sleep(5)
    print(f"[LOGIN] Page loaded: {page.url}")

    # Step 2: Fill login form
    print("[LOGIN] Filling credentials...")
    filled = False

    # Try multiple selector strategies
    selectors_user = [
        "input[name='login']",
        "input[name='user']",
        "input[name='username']",
        "input[type='text']:visible",
        "input[placeholder*='Login']",
        "input[placeholder*='login']",
        "input[placeholder*='Email']",
    ]

    selectors_pass = [
        "input[name='password']",
        "input[type='password']:visible",
        "input[placeholder*='Password']",
        "input[placeholder*='password']",
    ]

    for sel in selectors_user:
        try:
            await page.fill(sel, XBET_USERNAME, timeout=5000)
            print(f"[LOGIN] Username filled with: {sel}")
            filled = True
            break
        except:
            continue

    if not filled:
        # Take screenshot to debug
        ss = await page.screenshot(type="jpeg", quality=70)
        b64 = base64.standard_b64encode(ss).decode()
        data_store["debug_screenshot"] = b64
        raise Exception("Could not find username input field")

    for sel in selectors_pass:
        try:
            await page.fill(sel, XBET_PASSWORD, timeout=5000)
            print(f"[LOGIN] Password filled with: {sel}")
            break
        except:
            continue

    # Submit
    try:
        await page.click("button[type='submit']", timeout=10000)
    except:
        await page.keyboard.press("Enter")

    print("[LOGIN] Submitted, waiting for redirect...")
    await asyncio.sleep(8)
    print(f"[LOGIN] After login URL: {page.url}")

    # Step 3: Navigate to Mega Sic Bo
    print("[NAV] Navigating to Mega Sic Bo...")

    # Try direct game URLs
    game_urls = [
        "https://1xbet.com/en/live-casino/game/mega-sic-bo",
        "https://1xbet.com/en/casino/game/pragmatic-play-mega-sic-bo",
        "https://1xbet.com/en/live/casino",
    ]

    for url in game_urls:
        try:
            print(f"[NAV] Trying: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            print(f"[NAV] Loaded: {page.url}")
            break
        except Exception as e:
            print(f"[NAV] Failed {url}: {e}")
            continue

    browser_context = context
    page_ref = page
    print("[NAV] Browser ready!")
    return page


async def capture_and_ocr():
    global page_ref
    if not page_ref:
        return None

    screenshot_bytes = await page_ref.screenshot(
        full_page=False,
        type="jpeg",
        quality=85
    )
    b64 = base64.standard_b64encode(screenshot_bytes).decode()

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                },
                {
                    "type": "text",
                    "text": """This is a 1xBet Mega Sic Bo live casino screenshot.

Extract:
1. DICE TOTAL - main number (4-17) in the circle
2. CHIP SEQUENCE - bottom row like "3 3 4 10" or "1 3 6 10 25x"
3. RESULT TYPE - BIG(11-17), SMALL(4-10), ODD, EVEN, or TRIPLE
4. GAME ID - ID number shown
5. BALANCE - Rs amount

Return ONLY valid JSON:
{
  "dice_total": <integer or null>,
  "big_small": "<BIG|SMALL|ODD|EVEN|TRIPLE|null>",
  "chips": "<string or null>",
  "game_id": "<string or null>",
  "balance": "<string or null>",
  "game_visible": <true/false>
}"""
                }
            ]
        }]
    )

    text = message.content[0].text
    clean = text.replace("```json", "").replace("```", "").strip()
    result = json.loads(clean)
    result["screenshot_b64"] = b64
    return result


async def scan_loop():
    global data_store

    async with async_playwright() as playwright:
        try:
            data_store["status"] = "logging_in"
            data_store["error"] = None
            await login_and_navigate(playwright)
            data_store["status"] = "scanning"

            last_game_id = None
            last_total = None

            while data_store["scanning"]:
                try:
                    result = await capture_and_ocr()

                    if result and result.get("game_visible"):
                        dice_total = result.get("dice_total")
                        game_id = result.get("game_id")
                        chips = result.get("chips")

                        is_new = (
                            dice_total is not None and
                            (game_id != last_game_id or dice_total != last_total)
                        )

                        if is_new:
                            entry = {
                                "id": len(data_store["rounds"]) + 1,
                                "dice_total": dice_total,
                                "big_small": result.get("big_small"),
                                "chips": chips,
                                "game_id": game_id,
                                "balance": result.get("balance"),
                                "timestamp": datetime.now().isoformat(),
                                "time": datetime.now().strftime("%H:%M:%S"),
                            }
                            data_store["rounds"].insert(0, entry)
                            if len(data_store["rounds"]) > 500:
                                data_store["rounds"] = data_store["rounds"][:500]

                            last_game_id = game_id
                            last_total = dice_total
                            print(f"[DATA] Round {entry['id']}: {dice_total} {result.get('big_small')} | {chips}")

                        data_store["last_scan"] = datetime.now().isoformat()

                    else:
                        print(f"[SCAN] Game not visible yet...")

                except Exception as e:
                    print(f"[SCAN ERROR] {e}")
                    data_store["error"] = str(e)

                await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"[FATAL] {e}")
            data_store["status"] = "error"
            data_store["error"] = str(e)
            data_store["scanning"] = False


def run_async_loop():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scan_loop())


@app.route("/")
def index():
    return jsonify({"service": "SicBo Collector", "status": data_store["status"]})


@app.route("/api/start", methods=["POST"])
def start_scan():
    if data_store["scanning"]:
        return jsonify({"ok": False, "msg": "Already scanning"})
    if not XBET_USERNAME or not XBET_PASSWORD:
        return jsonify({"ok": False, "msg": "XBET_USERNAME / XBET_PASSWORD not set"})

    data_store["scanning"] = True
    data_store["status"] = "starting"
    data_store["error"] = None

    t = threading.Thread(target=run_async_loop, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Scan started"})


@app.route("/api/stop", methods=["POST"])
def stop_scan():
    data_store["scanning"] = False
    data_store["status"] = "idle"
    return jsonify({"ok": True})


@app.route("/api/status")
def get_status():
    return jsonify({
        "status": data_store["status"],
        "scanning": data_store["scanning"],
        "total_rounds": len(data_store["rounds"]),
        "last_scan": data_store["last_scan"],
        "error": data_store["error"]
    })


@app.route("/api/rounds")
def get_rounds():
    limit = int(request.args.get("limit", 100))
    rounds = data_store["rounds"][:limit]
    return jsonify({"rounds": rounds, "total": len(data_store["rounds"])})


@app.route("/api/rounds/clear", methods=["POST"])
def clear_rounds():
    data_store["rounds"] = []
    return jsonify({"ok": True})


@app.route("/api/export/csv")
def export_csv():
    rows = data_store["rounds"]
    lines = ["Round,Dice Total,Big/Small,Chips,Game ID,Time"]
    for r in reversed(rows):
        lines.append(f"{r['id']},{r['dice_total']},{r.get('big_small','')},{r.get('chips','')},{r.get('game_id','')},{r['time']}")
    csv_data = "\n".join(lines)
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=sicbo_data.csv"})


@app.route("/api/debug/screenshot")
def debug_screenshot():
    """Get latest screenshot for debugging"""
    ss = data_store.get("debug_screenshot")
    if ss:
        return jsonify({"screenshot": ss})
    return jsonify({"error": "No screenshot available"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
