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
XBET_USERNAME = os.environ.get("XBET_USERNAME", "")
XBET_PASSWORD = os.environ.get("XBET_PASSWORD", "")
GAME_URL = "https://lk.1xbet.com/en/casino-search?game=56264"

page_ref = None
loop = None


async def inject_cookies(context):
    """Inject cookies to all possible domains including mirror"""
    try:
        cookies_raw = json.loads(XBET_COOKIES_JSON)
        pw_cookies = []
        mirror_domains = [
            '.1xlite-03864.pro', '1xlite-03864.pro',
            'lk.1xbet.com', '.1xbet.com'
        ]
        for c in cookies_raw:
            for domain in mirror_domains:
                pw_cookies.append({
                    'name': c['name'],
                    'value': c['value'],
                    'domain': domain,
                    'path': '/'
                })
        await context.add_cookies(pw_cookies)
        print(f"[COOKIES] Injected to all domains")
        return True
    except Exception as e:
        print(f"[COOKIES ERROR] {e}")
        return False


async def do_login(page):
    """Login with username/password as fallback"""
    print("[LOGIN] Trying credentials login...")
    try:
        await page.goto("https://lk.1xbet.com/en/login",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        await page.evaluate(f"""
            () => {{
                const inputs = document.querySelectorAll('input');
                let u = false, p = false;
                for(const inp of inputs) {{
                    const t = inp.type.toLowerCase();
                    const n = (inp.name || inp.placeholder || '').toLowerCase();
                    if(!u && (t==='text'||t==='email'||n.includes('login')||n.includes('user'))) {{
                        inp.value = '{XBET_USERNAME}';
                        inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                        u = true;
                    }} else if(!p && t==='password') {{
                        inp.value = '{XBET_PASSWORD}';
                        inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                        p = true;
                    }}
                }}
            }}
        """)
        await asyncio.sleep(1)
        try:
            await page.click("button[type='submit']", timeout=8000)
        except:
            await page.keyboard.press("Enter")
        await asyncio.sleep(6)
        print(f"[LOGIN] URL after login: {page.url}")
        return True
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        return False


async def setup_browser(playwright):
    global page_ref

    print("[BROWSER] Launching...")
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
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True
    )

    # Inject cookies to all domains including mirror
    await inject_cookies(context)

    page = await context.new_page()
    page.set_default_timeout(120000)

    # Navigate to game
    print(f"[NAV] Opening: {GAME_URL}")
    await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(6)

    current_url = page.url
    print(f"[NAV] Loaded: {current_url}")

    # Check login status
    page_text = await page.evaluate("() => document.body.innerText")
    is_logged_in = "Please log in" not in page_text and "LOG IN" not in page_text[:200]
    print(f"[NAV] Logged in: {is_logged_in}")

    if not is_logged_in:
        print("[NAV] Not logged in, trying credentials...")
        await do_login(page)
        await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

    # Screenshot for debug
    ss = await page.screenshot(type="jpeg", quality=70)
    data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()

    # Update mirror domain for future use
    data_store["mirror_url"] = page.url

    page_ref = page
    print("[NAV] Ready to scan!")
    return page


async def scrape_sic_bo():
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
        # Screenshot
        ss = await page_ref.screenshot(type="jpeg", quality=60)
        data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()

        # Check session
        page_text = await page_ref.evaluate("() => document.body.innerText")
        if "Please log in" in page_text:
            print("[SCAN] Session expired! Re-logging in...")
            data_store["status"] = "re-login"
            await do_login(page_ref)
            await page_ref.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(8)
            page_text = await page_ref.evaluate("() => document.body.innerText")
            data_store["status"] = "scanning"

        # Balance
        bal = re.search(r'Rs\s*([\d,]+\.?\d*)', page_text)
        if bal:
            result["balance"] = "Rs " + bal.group(1)

        # Scan all frames for Sic Bo
        frames = page_ref.frames
        for frame in frames:
            try:
                furl = frame.url
                if not furl or furl == "about:blank":
                    continue

                frame_text = await frame.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )

                if not re.search(
                    r'SIC.?BO|ANY.?TRIPLE|4\s*[-–]\s*10|11\s*[-–]\s*17|SMALL.*4|BIG.*11',
                    frame_text, re.IGNORECASE
                ):
                    continue

                print(f"[FRAME] Sic Bo in: {furl[:60]}")
                result["game_visible"] = True

                # Result type
                if re.search(r'\bTRIPLE\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "TRIPLE"
                elif re.search(r'\bBIG\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "BIG"
                elif re.search(r'\bSMALL\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "SMALL"
                elif re.search(r'\bODD\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "ODD"
                elif re.search(r'\bEVEN\b', frame_text, re.IGNORECASE):
                    result["big_small"] = "EVEN"

                # Chip sequence
                chip = re.search(
                    r'\b([1-9])\s+([1-9])\s+([1-9])\s+(\d+)(?:\s+(\d+x?))?\b',
                    frame_text
                )
                if chip:
                    parts = [chip.group(i) for i in range(1,6) if chip.group(i)]
                    result["chips"] = " ".join(parts)

                # Game ID
                gid = re.search(r'ID[:\s#]*(\d{8,})', frame_text)
                if gid:
                    result["game_id"] = gid.group(1)

                # Dice total — largest font number 4-17
                nums = await frame.evaluate("""
                    () => {
                        const res = [];
                        for(const el of document.querySelectorAll('*')) {
                            if(el.children.length === 0 && el.innerText) {
                                const t = el.innerText.trim();
                                const n = parseInt(t);
                                if(!isNaN(n) && n>=4 && n<=17 && t===String(n)) {
                                    const r = el.getBoundingClientRect();
                                    const fs = parseFloat(window.getComputedStyle(el).fontSize)||12;
                                    if(r.width>10 && r.height>10)
                                        res.push({num:n, fs:fs, area:r.width*r.height});
                                }
                            }
                        }
                        res.sort((a,b)=>(b.fs-a.fs)||(b.area-a.area));
                        return res.slice(0,5);
                    }
                """)

                if nums:
                    print(f"[DOM] Numbers: {nums}")
                    result["dice_total"] = nums[0]["num"]

                # Auto BIG/SMALL
                if result["dice_total"] and not result["big_small"]:
                    result["big_small"] = "BIG" if result["dice_total"] >= 11 else "SMALL"

                break

            except Exception as e:
                print(f"[FRAME ERR] {e}")
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
                            print(f"[DATA] #{entry['id']}: {dice_total} {result.get('big_small')} | {chips}")

                        data_store["last_scan"] = datetime.now().isoformat()
                        data_store["status"] = "scanning"
                    else:
                        print("[SCAN] Game not visible...")

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
    data_store["scanning"] = True
    data_store["status"] = "starting"
    data_store["error"] = None
    threading.Thread(target=run_async_loop, daemon=True).start()
    return jsonify({"ok": True})


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
        "error": data_store["error"],
        "mirror": data_store.get("mirror_url", "")
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
    return jsonify({"error": "No screenshot yet"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
