import os
import json
import base64
import asyncio
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from playwright.async_api import async_playwright
import anthropic

app = Flask(__name__)
CORS(app)

# In-memory storage (persists while Railway container runs)
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
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "15"))  # seconds

# Global browser state
browser_context = None
page_ref = None
loop = None


async def login_and_navigate(playwright):
    """Login to 1xBet and navigate to Mega Sic Bo"""
    global browser_context, page_ref

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,720"
        ]
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    page = await context.new_page()

    # Go to 1xBet
    print("[LOGIN] Navigating to 1xbet.com...")
    await page.goto("https://1xbet.com/en/live/casino", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)

    # Click login button
    try:
        await page.click("text=Log in", timeout=110000)
        await asyncio.sleep(2)
    except:
        try:
            await page.click("[class*='login']", timeout=15000)
            await asyncio.sleep(2)
        except:
            print("[LOGIN] Login button not found via text, trying direct URL...")
            await page.goto("https://1xbet.com/en/login", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

    # Fill credentials
    print("[LOGIN] Entering credentials...")
    try:
        await page.fill("input[name='login']", XBET_USERNAME, timeout=110000)
        await page.fill("input[name='password']", XBET_PASSWORD, timeout=110000)
        await page.click("button[type='submit']", timeout=110000)
    except:
        # Try alternative selectors
        await page.fill("input[type='text']", XBET_USERNAME, timeout=110000)
        await page.fill("input[type='password']", XBET_PASSWORD, timeout=110000)
        await page.keyboard.press("Enter")

    await asyncio.sleep(5)
    print("[LOGIN] Login submitted, waiting...")

    # Navigate to Mega Sic Bo
    print("[NAV] Searching for Mega Sic Bo...")
    await page.goto(
        "https://1xbet.com/en/live/casino",
        wait_until="domcontentloaded",
        timeout=130000
    )
    await asyncio.sleep(3)

    # Search for Mega Sic Bo
    try:
        await page.fill("input[placeholder*='Search']", "Mega Sic Bo", timeout=8000)
        await asyncio.sleep(2)
        await page.click("text=Mega Sic Bo", timeout=18000)
        await asyncio.sleep(5)
    except:
        print("[NAV] Search failed, trying direct navigation...")
        # Try Pragmatic Play Mega Sic Bo direct URL patterns
        urls_to_try = [
            "https://1xbet.com/en/casino/game/pragmatic-play-mega-sic-bo",
            "https://1xbet.com/en/live-casino/game/mega-sic-bo",
        ]
        for url in urls_to_try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
                break
            except:
                continue

    browser_context = context
    page_ref = page
    print("[NAV] Ready to scan!")
    return page


async def capture_and_ocr():
    """Take screenshot and extract Sic Bo data via Claude"""
    global page_ref

    if not page_ref:
        return None

    # Take screenshot
    screenshot_bytes = await page_ref.screenshot(
        full_page=False,
        type="jpeg",
        quality=85
    )

    b64 = base64.standard_b64encode(screenshot_bytes).decode()

    # Claude OCR
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64
                    }
                },
                {
                    "type": "text",
                    "text": """This is a 1xBet Mega Sic Bo live casino screenshot.

Extract these values:
1. DICE TOTAL - the main number shown (4-17), visible in the spinning circle/display
2. CHIP SEQUENCE - bottom row numbers like "3 3 4 10" or "1 3 6 10 25x"  
3. RESULT TYPE - BIG(11-17), SMALL(4-10), ODD, EVEN, or TRIPLE
4. GAME ID - the ID number shown (e.g. 15607313722)
5. BALANCE - Rs amount shown

Return ONLY valid JSON:
{
  "dice_total": <integer or null>,
  "big_small": "<BIG|SMALL|ODD|EVEN|TRIPLE|null>",
  "chips": "<string like '3 3 4 10' or null>",
  "game_id": "<string or null>",
  "balance": "<string or null>",
  "game_visible": <true if Sic Bo game is visible, false otherwise>
}

If game result not yet shown (dice rolling), set dice_total to null."""
                }
            ]
        }]
    )

    text = message.content[0].text
    clean = text.replace("```json", "").replace("```", "").strip()
    result = json.loads(clean)
    result["screenshot_b64"] = b64  # store for frontend preview
    return result


async def scan_loop():
    """Main scanning loop"""
    global data_store

    async with async_playwright() as playwright:
        try:
            data_store["status"] = "logging_in"
            await login_and_navigate(playwright)
            data_store["status"] = "scanning"
            data_store["error"] = None

            last_game_id = None
            last_total = None

            while data_store["scanning"]:
                try:
                    result = await capture_and_ocr()

                    if result and result.get("game_visible"):
                        dice_total = result.get("dice_total")
                        game_id = result.get("game_id")
                        chips = result.get("chips")

                        # Only save if new round (different game_id or different total)
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
                                "preview": result.get("screenshot_b64", "")[:100]  # truncate for storage
                            }
                            data_store["rounds"].insert(0, entry)

                            # Keep last 500 rounds
                            if len(data_store["rounds"]) > 500:
                                data_store["rounds"] = data_store["rounds"][:500]

                            last_game_id = game_id
                            last_total = dice_total
                            print(f"[DATA] Round {entry['id']}: Total={dice_total} {result.get('big_small')} | Chips: {chips}")

                        data_store["last_scan"] = datetime.now().isoformat()

                    elif result and not result.get("game_visible"):
                        print("[SCAN] Game not visible, waiting...")

                except Exception as e:
                    print(f"[SCAN ERROR] {e}")
                    data_store["error"] = str(e)

                # Wait for next scan
                await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"[FATAL] {e}")
            data_store["status"] = "error"
            data_store["error"] = str(e)
            data_store["scanning"] = False


def run_async_loop():
    """Run async scan loop in background thread"""
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scan_loop())


# ─── API Routes ───────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"service": "SicBo Collector", "status": data_store["status"]})


@app.route("/api/start", methods=["POST"])
def start_scan():
    if data_store["scanning"]:
        return jsonify({"ok": False, "msg": "Already scanning"})

    if not XBET_USERNAME or not XBET_PASSWORD:
        return jsonify({"ok": False, "msg": "XBET_USERNAME / XBET_PASSWORD env vars not set"})

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
    return jsonify({"ok": True, "msg": "Scan stopped"})


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
    # Remove large screenshot data from list response
    clean = [{k: v for k, v in r.items() if k != "preview"} for r in rounds]
    return jsonify({"rounds": clean, "total": len(data_store["rounds"])})


@app.route("/api/rounds/clear", methods=["POST"])
def clear_rounds():
    data_store["rounds"] = []
    return jsonify({"ok": True})


@app.route("/api/export/csv")
def export_csv():
    from flask import Response
    rows = data_store["rounds"]
    lines = ["Round,Dice Total,Big/Small,Chips,Game ID,Time"]
    for r in reversed(rows):
        lines.append(f"{r['id']},{r['dice_total']},{r['big_small']},{r.get('chips','')},{r.get('game_id','')},{r['time']}")
    csv = "\n".join(lines)
    return Response(csv, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=sicbo_data.csv"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
