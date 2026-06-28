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
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "15"))
XBET_COOKIES_JSON = os.environ.get("XBET_COOKIES", "[]")

browser_context = None
page_ref = None
loop = None


async def setup_browser_with_cookies(playwright):
    global browser_context, page_ref

    print("[BROWSER] Launching Chromium...")
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,800"
        ]
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True
    )

    # Inject cookies
    print("[COOKIES] Injecting session cookies...")
    try:
        cookies_raw = json.loads(XBET_COOKIES_JSON)
        playwright_cookies = []
        for c in cookies_raw:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            if not c.get("session", True) and c.get("expirationDate"):
                cookie["expires"] = int(c["expirationDate"])
            if c.get("sameSite") and c["sameSite"] not in [None, "null", "no_restriction"]:
                same = c["sameSite"].capitalize()
                if same in ["Strict", "Lax", "None"]:
                    cookie["sameSite"] = same
            playwright_cookies.append(cookie)

        await context.add_cookies(playwright_cookies)
        print(f"[COOKIES] Injected {len(playwright_cookies)} cookies")
    except Exception as e:
        print(f"[COOKIES ERROR] {e}")
        raise Exception(f"Cookie injection failed: {e}")

    page = await context.new_page()
    page.set_default_timeout(120000)

    # Navigate directly to Mega Sic Bo
    print("[NAV] Opening Mega Sic Bo...")
    game_urls = [
        "https://lk.1xbet.com/en/live-casino/game/mega-sic-bo",
        "https://lk.1xbet.com/en/casino/game/pragmatic-play-mega-sic-bo",
        "https://lk.1xbet.com/en/live/casino",
    ]

    for url in game_urls:
        try:
            print(f"[NAV] Trying: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            current = page.url
            print(f"[NAV] Loaded: {current}")
            # Check if redirected to login (not logged in)
            if "login" in current or "signin" in current:
                print("[NAV] Redirected to login — cookies may be expired")
                raise Exception("Cookies expired — please export fresh cookies")
            break
        except Exception as e:
            print(f"[NAV] Failed {url}: {e}")
            if "expired" in str(e):
                raise e
            continue

    browser_context = context
    page_ref = page
    print("[NAV] Ready to scan!")
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
    data_store["last_screenshot"] = b64  # store for debug

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
1. DICE TOTAL - main number (4-17) in the spinning circle at top
2. CHIP SEQUENCE - bottom row numbers like "3 3 4 10" or "1 3 6 10 25x"
3. RESULT TYPE - BIG(11-17), SMALL(4-10), ODD, EVEN, or TRIPLE
4. GAME ID - ID number shown (e.g. 15607313722)
5. BALANCE - Rs amount shown

Return ONLY valid JSON:
{
  "dice_total": <integer or null>,
  "big_small": "<BIG|SMALL|ODD|EVEN|TRIPLE|null>",
  "chips": "<string like '3 3 4 10' or null>",
  "game_id": "<string or null>",
  "balance": "<string or null>",
  "game_visible": <true if Sic Bo table visible, false otherwise>
}"""
                }
            ]
        }]
    )

    text = message.content[0].text
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


async def scan_loop():
    global data_store

    async with async_playwright() as playwright:
        try:
            data_store["status"] = "connecting"
            data_store["error"] = None
            await setup_browser_with_cookies(playwright)
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
                        print("[SCAN] Game not visible yet, waiting...")

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
    if not XBET_COOKIES_JSON or XBET_COOKIES_JSON == "[]":
        return jsonify({"ok": False, "msg": "XBET_COOKIES env var not set"})

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
    return jsonify({"rounds": data_store["rounds"][:limit], "total": len(data_store["rounds"])})


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
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=sicbo_data.csv"})


@app.route("/api/debug/screenshot")
def debug_screenshot():
    ss = data_store.get("last_screenshot")
    if ss:
        # Return as HTML image for easy viewing
        return f'<img src="data:image/jpeg;base64,{ss}" style="max-width:100%">'
    return jsonify({"error": "No screenshot yet"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
