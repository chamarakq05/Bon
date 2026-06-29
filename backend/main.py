import os
import json
import base64
import asyncio
import threading
import re
import time
from datetime import datetime
from flask import Flask, jsonify, request, Response, redirect
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

data_store = {
    "rounds": [],
    "status": "idle",
    "last_scan": None,
    "error": None,
    "scanning": False,
    "last_screenshot": None,
    "last_result": None
}

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))
XBET_COOKIES_JSON = os.environ.get("XBET_COOKIES", "[]")
XBET_USERNAME = os.environ.get("XBET_USERNAME", "")
XBET_PASSWORD = os.environ.get("XBET_PASSWORD", "")
GAME_URL = "https://1xlite-03864.pro/en/casino-search?game=56264&platform_type=mobile"

page_ref = None
browser_ref = None
loop = None
playwright_ref = None


async def inject_cookies(context):
    try:
        cookies_raw = json.loads(XBET_COOKIES_JSON)
        pw_cookies = []
        for c in cookies_raw:
            for domain in [c['domain'], '.1xlite-03864.pro', '1xlite-03864.pro', 'lk.1xbet.com', '.1xbet.com']:
                pw_cookies.append({
                    'name': c['name'],
                    'value': c['value'],
                    'domain': domain,
                    'path': '/'
                })
        await context.add_cookies(pw_cookies)
        print(f"[COOKIES] Injected to all domains")
    except Exception as e:
        print(f"[COOKIES ERROR] {e}")


async def start_browser():
    global page_ref, browser_ref, playwright_ref

    from playwright.async_api import async_playwright
    playwright_ref = await async_playwright().start()

    print("[BROWSER] Launching...")
    browser_ref = await playwright_ref.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1280,800"
        ]
    )

    context = await browser_ref.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True
    )

    await inject_cookies(context)
    page = await context.new_page()
    page.set_default_timeout(120000)

    print(f"[NAV] Loading game page...")
    await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)

    # Check login
    page_text = await page.evaluate("() => document.body.innerText")
    if "Please log in" in page_text or "LOG IN" in page_text[:300]:
        print("[LOGIN] Not logged in, trying credentials...")
        await page.goto("https://lk.1xbet.com/en/login", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)
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
        await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

    ss = await page.screenshot(type="jpeg", quality=70)
    data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()

    page_ref = page
    print("[BROWSER] Ready!")
    return page


async def take_screenshot_and_extract():
    global page_ref
    if not page_ref:
        return None

    try:
        ss = await page_ref.screenshot(type="jpeg", quality=75)
        data_store["last_screenshot"] = base64.standard_b64encode(ss).decode()

        # Try DOM extraction first (free)
        result = await extract_from_dom()
        if result and result.get("game_visible"):
            return result

        return {"game_visible": False, "dice_total": None, "big_small": None, "chips": None}

    except Exception as e:
        print(f"[SCREENSHOT ERROR] {e}")
        return None


async def extract_from_dom():
    global page_ref
    result = {
        "dice_total": None,
        "big_small": None,
        "chips": None,
        "game_id": None,
        "balance": None,
        "game_visible": False
    }

    try:
        page_text = await page_ref.evaluate("() => document.body.innerText")

        # Check session
        if "Please log in" in page_text:
            data_store["error"] = "Session expired — please refresh cookies"
            return result

        # Balance
        bal = re.search(r'Rs\s*([\d,]+\.?\d*)', page_text)
        if bal:
            result["balance"] = "Rs " + bal.group(1)

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
                    r'SIC.?BO|ANY.?TRIPLE|4\s*[-–]\s*10|11\s*[-–]\s*17',
                    frame_text, re.IGNORECASE
                ):
                    continue

                print(f"[FRAME] Sic Bo found!")
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
                    parts = [chip.group(i) for i in range(1, 6) if chip.group(i)]
                    result["chips"] = " ".join(parts)

                # Game ID
                gid = re.search(r'ID[:\s#]*(\d{8,})', frame_text)
                if gid:
                    result["game_id"] = gid.group(1)

                # Dice total
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
                    result["dice_total"] = nums[0]["num"]
                    if not result["big_small"]:
                        result["big_small"] = "BIG" if result["dice_total"] >= 11 else "SMALL"

                break

            except Exception as e:
                continue

    except Exception as e:
        print(f"[DOM ERROR] {e}")

    return result


async def scan_loop():
    global data_store

    try:
        data_store["status"] = "connecting"
        data_store["error"] = None
        await start_browser()
        data_store["status"] = "scanning"

        last_game_id = None
        last_total = None

        while data_store["scanning"]:
            try:
                result = await take_screenshot_and_extract()

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
                        data_store["last_result"] = entry
                        print(f"[DATA] #{entry['id']}: {dice_total} {result.get('big_small')} | {chips}")

                    data_store["last_scan"] = datetime.now().isoformat()
                    data_store["status"] = "scanning"

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


# ── API Routes ──────────────────────────────────────

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
        "last_result": data_store.get("last_result")
    })


@app.route("/api/rounds")
def get_rounds():
    limit = int(request.args.get("limit", 100))
    return jsonify({
        "rounds": data_store["rounds"][:limit],
        "total": len(data_store["rounds"])
    })


@app.route("/api/rounds/clear", methods=["POST"])
def clear_rounds():
    data_store["rounds"] = []
    return jsonify({"ok": True})


@app.route("/api/export/csv")
def export_csv():
    rows = data_store["rounds"]
    lines = ["Round,Dice Total,Big/Small,Chips,Game ID,Time"]
    for r in reversed(rows):
        lines.append(
            f"{r['id']},{r.get('dice_total','')},{r.get('big_small','')}"
            f",{r.get('chips','')},{r.get('game_id','')},{r['time']}"
        )
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=sicbo_data.csv"}
    )


@app.route("/api/debug/screenshot")
def debug_screenshot():
    ss = data_store.get("last_screenshot")
    if ss:
        return (
            f'<html><body style="margin:0;background:#000">'
            f'<img src="data:image/jpeg;base64,{ss}" style="max-width:100%;height:auto">'
            f'</body></html>'
        )
    return jsonify({"error": "No screenshot yet"})


# ── Upload endpoint — phone screenshots ────────────

@app.route("/api/upload", methods=["POST"])
def upload_screenshot():
    """Receive screenshot from phone and extract data"""
    try:
        if 'image' in request.files:
            img_data = request.files['image'].read()
        elif request.data:
            img_data = request.data
        else:
            return jsonify({"ok": False, "msg": "No image data"})

        b64 = base64.standard_b64encode(img_data).decode()
        data_store["last_screenshot"] = b64

        # Extract data using Claude Vision if API key available
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                        },
                        {
                            "type": "text",
                            "text": """1xBet Mega Sic Bo screenshot. Extract:
1. DICE TOTAL (4-17, big number in circle)
2. CHIP SEQUENCE bottom row (e.g. "3 3 4 10")
3. BIG/SMALL/ODD/EVEN/TRIPLE
4. GAME ID
5. BALANCE (Rs amount)

Return ONLY JSON:
{"dice_total":<int|null>,"big_small":"<BIG|SMALL|ODD|EVEN|TRIPLE|null>","chips":"<str|null>","game_id":"<str|null>","balance":"<str|null>","game_visible":<bool>}"""
                        }
                    ]
                }]
            )
            text = message.content[0].text
            clean = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(clean)
        else:
            return jsonify({"ok": False, "msg": "No ANTHROPIC_API_KEY set"})

        if result and result.get("game_visible") and result.get("dice_total"):
            entry = {
                "id": len(data_store["rounds"]) + 1,
                "dice_total": result["dice_total"],
                "big_small": result.get("big_small"),
                "chips": result.get("chips"),
                "game_id": result.get("game_id"),
                "balance": result.get("balance"),
                "timestamp": datetime.now().isoformat(),
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            data_store["rounds"].insert(0, entry)
            data_store["last_result"] = entry
            data_store["last_scan"] = datetime.now().isoformat()
            print(f"[UPLOAD] #{entry['id']}: {entry['dice_total']} {entry['big_small']}")
            return jsonify({"ok": True, "data": entry})

        return jsonify({"ok": True, "data": result, "saved": False})

    except Exception as e:
        print(f"[UPLOAD ERROR] {e}")
        return jsonify({"ok": False, "msg": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
