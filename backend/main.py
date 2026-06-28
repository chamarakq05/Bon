import os
import json
import base64
import asyncio
import threading
import re
from datetime import datetime
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from playwright.async_api import async_playwright

app = Flask(__name__)
CORS(app)

data_store = {
    "rounds": [],
    "status": "idle",
    "last_scan": None,
    "error": None,
    "scanning": False,
    "last_screenshot": None
}

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))
XBET_COOKIES_JSON = os.environ.get("XBET_COOKIES", "[]")

browser_context = None
page_ref = None
loop = None


async def setup_browser(playwright):
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
    print("[COOKIES] Injecting cookies...")
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
            ss = c.get("sameSite")
            if ss and ss not in [None, "null", "no_restriction"]:
                mapped = ss.capitalize()
                if mapped in ["Strict", "Lax", "None"]:
                    cookie["sameSite"] = mapped
            playwright_cookies.append(cookie)

        await context.add_cookies(playwright_cookies)
        print(f"[COOKIES] Injected {len(playwright_cookies)} cookies")
    except Exception as e:
        raise Exception(f"Cookie injection failed: {e}")

    page = await context.new_page()
    page.set_default_timeout(120000)

    # Navigate to Mega Sic Bo
    game_urls = [
    "https://lk.1xbet.com/en/casino-search?game=56264",
    "https://lk.1xbet.com/en/live-casino",
]

    loaded = False
    for url in game_urls:
        try:
            print(f"[NAV] Trying: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(6)
            current = page.url
            print(f"[NAV] Loaded: {current}")
            if "login" in current or "block" in current or "signin" in current:
                print(f"[NAV] Blocked/redirected at {url}, trying next...")
                continue
            loaded = True
            break
        except Exception as e:
            print(f"[NAV] Failed {url}: {e}")
            continue

    if not loaded:
        # Take screenshot for debug
        ss = await page.screenshot(type="jpeg", quality=70)
        data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()
        raise Exception("Could not load Mega Sic Bo page — may be geo-blocked or cookies expired")

    # Wait for game iframe to load
    print("[NAV] Waiting for game to load...")
    await asyncio.sleep(8)

    browser_context = context
    page_ref = page
    print("[NAV] Ready!")
    return page


async def scrape_sic_bo():
    """Extract Sic Bo data from DOM — no API cost"""
    global page_ref
    if not page_ref:
        return None

    result = {
        "dice_total": None,
        "big_small": None,
        "chips": None,
        "game_id": None,
        "balance": None,
        "game_visible": False
    }

    try:
        # Take screenshot for debug
        ss = await page_ref.screenshot(type="jpeg", quality=70)
        data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()

        # Try to get data from page DOM
        page_text = await page_ref.evaluate("() => document.body.innerText")

        # Extract balance
        balance_match = re.search(r'Rs\s*([\d,]+\.?\d*)', page_text)
        if balance_match:
            result["balance"] = "Rs " + balance_match.group(1)

        # Extract game ID
        id_match = re.search(r'ID[:\s]*(\d{10,})', page_text)
        if id_match:
            result["game_id"] = id_match.group(1)

        # Try to find iframe with game content
        frames = page_ref.frames
        print(f"[DOM] Found {len(frames)} frames")

        for frame in frames:
            try:
                frame_url = frame.url
                if not frame_url or frame_url == "about:blank":
                    continue
                print(f"[FRAME] {frame_url[:80]}")

                frame_text = await frame.evaluate("() => document.body ? document.body.innerText : ''")

                # Look for dice total (number between 4-17 prominently displayed)
                # Sic Bo shows result as standalone number
                numbers = re.findall(r'\b([4-9]|1[0-7])\b', frame_text)

                # Look for chip sequences like "3 3 4 10" or "1 3 6 10"
                chip_match = re.search(r'\b([1-9])\s+([1-9])\s+([1-9])\s+(\d+)\b', frame_text)
                if chip_match:
                    result["chips"] = f"{chip_match.group(1)} {chip_match.group(2)} {chip_match.group(3)} {chip_match.group(4)}"

                # Look for BIG/SMALL/ODD/EVEN/TRIPLE
                if re.search(r'\bBIG\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "BIG"
                elif re.search(r'\bSMALL\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "SMALL"
                elif re.search(r'\bTRIPLE\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "TRIPLE"
                elif re.search(r'\bODD\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "ODD"
                elif re.search(r'\bEVEN\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "EVEN"

                # Check if Sic Bo game is visible
                if re.search(r'SIC\s*BO|SMALL|BIG|TRIPLE|ANY\s*TRIPLE', frame_text, re.IGNORECASE):
                    result["game_visible"] = True

                    # Try to extract dice total from DOM elements
                    try:
                        # Common selectors for result numbers in Pragmatic Play games
                        selectors = [
                            "[class*='result'] [class*='number']",
                            "[class*='dice-total']",
                            "[class*='total-number']",
                            "[class*='result-number']",
                            "[class*='game-result']",
                            "[data-total]",
                        ]
                        for sel in selectors:
                            try:
                                el = await frame.query_selector(sel)
                                if el:
                                    txt = await el.inner_text()
                                    num = int(txt.strip())
                                    if 4 <= num <= 17:
                                        result["dice_total"] = num
                                        break
                            except:
                                continue

                        # If selector failed, try JS evaluation
                        if result["dice_total"] is None:
                            # Look for the main displayed number
                            all_nums = await frame.evaluate("""
                                () => {
                                    const els = document.querySelectorAll('*');
                                    const results = [];
                                    for(const el of els) {
                                        if(el.children.length === 0) {
                                            const t = el.innerText ? el.innerText.trim() : '';
                                            const n = parseInt(t);
                                            if(!isNaN(n) && n >= 4 && n <= 17 && t === String(n)) {
                                                const rect = el.getBoundingClientRect();
                                                if(rect.width > 20 && rect.height > 20) {
                                                    results.push({num: n, size: rect.width * rect.height});
                                                }
                                            }
                                        }
                                    }
                                    results.sort((a,b) => b.size - a.size);
                                    return results.slice(0, 5);
                                }
                            """)
                            if all_nums:
                                result["dice_total"] = all_nums[0]["num"]
                                print(f"[DOM] Found numbers: {all_nums}")

                    except Exception as e:
                        print(f"[DOM] Element extraction error: {e}")

                    # Determine BIG/SMALL from total if not found
                    if result["dice_total"] and not result["big_small"]:
                        t = result["dice_total"]
                        if t >= 11:
                            result["big_small"] = "BIG"
                        elif t <= 10:
                            result["big_small"] = "SMALL"

                    break

            except Exception as e:
                print(f"[FRAME] Error: {e}")
                continue

    except Exception as e:
        print(f"[SCRAPE ERROR] {e}")
        raise e

    return result


async def scan_loop():
    global data_store

    async with async_playwright() as playwright:
        try:
            data_store["status"] = "connecting"
            data_store["error"] = None
            await setup_browser(playwright)
            data_store["status"] = "scanning"

            last_game_id = None
            last_total = None

            while data_store["scanning"]:
                try:
                    result = await scrape_sic_bo()

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
                        print(f"[SCAN] Game not visible, waiting...")

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
        return jsonify({"ok": False, "msg": "XBET_COOKIES not set"})

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
        lines.append(f"{r['id']},{r.get('dice_total','')},{r.get('big_small','')},{r.get('chips','')},{r.get('game_id','')},{r['time']}")
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=sicbo_data.csv"})


@app.route("/api/debug/screenshot")
def debug_screenshot():
    ss = data_store.get("last_screenshot")
    if ss:
        return f'<html><body style="margin:0;background:#000"><img src="data:image/jpeg;base64,{ss}" style="max-width:100%;height:auto"></body></html>'
    return jsonify({"error": "No screenshot yet — start scanning first"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
