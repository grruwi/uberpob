"""Death Counter Overlay — monitors PoE Client.txt for deaths, generates AI roasts.

Usage:
    python death_counter.py

Serves overlay on http://localhost:6969 — add as OBS Browser Source.
Optionally announces deaths on Twitch chat.
"""

import os
import re
import asyncio
import json
import time
from pathlib import Path
from aiohttp import web
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# Config
CLIENT_TXT = Path(os.getenv(
    "POE_CLIENT_TXT",
    os.path.expanduser("~/.local/share/Steam/steamapps/common/Path of Exile/logs/Client.txt")
))
OVERLAY_PORT = int(os.getenv("DEATH_OVERLAY_PORT", "6969"))

# Gemini client
project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
gemini_client = genai.Client(vertexai=True, project=project_id, location=location)

ROAST_PROMPT = """Jesteś komentarzem sportowym Path of Exile. Gracz właśnie umarł.
Wygeneruj JEDEN krótki, sarkastyczny komentarz po polsku (max 15 słów).
Styl: komentator sportowy, trochę beka, trochę dramatyzm. Nie powtarzaj się.
Przykłady tonów: "TO JEST KATASTROFA", "szanowni państwo, to był samobójczy misklick",
"i oto nasze nadzieje olimpijskie gasną pod bosem", "RIP bozo xD"
NIE pisz nic poza samym komentarzem — zero cudzysłowów, zero wyjaśnień."""

# State — per-character death tracking
_char_deaths: dict[str, int] = {}  # char_name -> death count
current_char = ""  # Auto-detected from log
last_roast = ""
last_death_time = 0
_other_players: set[str] = set()  # Players who "joined the area" = NOT you
_clients: list[web.WebSocketResponse] = []


def death_count() -> int:
    """Current character's death count."""
    return _char_deaths.get(current_char, 0)


def _generate_roast_sync() -> str:
    """Synchronous roast generation — runs in thread to avoid event loop conflicts."""
    import traceback
    try:
        # Use synchronous client (not aio) to avoid event loop conflicts with aiohttp
        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[types.Content(role="user", parts=[types.Part(text="Gracz umarł. Skomentuj.")])],
            config=types.GenerateContentConfig(
                system_instruction=ROAST_PROMPT,
                temperature=1.5,
            ),
        )
        result = (response.text or "RIP").strip().strip('"\'')
        print(f"[Roast] Generated: {result}", flush=True)
        return result
    except Exception as e:
        print(f"[Roast] Error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return "Technicznie żyje, praktycznie nie."


async def generate_roast() -> str:
    """Generate a one-liner AI roast for a death event."""
    print("[Roast] Generating...", flush=True)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_roast_sync)


async def notify_clients():
    """Push death update to all connected WebSocket overlay clients."""
    data = json.dumps({"count": death_count(), "char": current_char, "roast": last_roast, "time": last_death_time})
    dead = []
    for ws in _clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.remove(ws)


async def tail_client_txt():
    """Watch Client.txt for death lines (tail -f style).

    Auto-detects which character is YOURS by tracking who "has joined the area"
    (those are other players). Any death from a name NOT in that set = your death.
    """
    global last_roast, last_death_time, current_char

    if not CLIENT_TXT.exists():
        print(f"[Death] Client.txt nie znaleziony: {CLIENT_TXT}", flush=True)
        print("[Death] Ustaw POE_CLIENT_TXT w .env lub uruchom PoE.", flush=True)
        return

    print(f"[Death] Monitoring {CLIENT_TXT} (from current position)", flush=True)
    print(f"[Death] Auto-detection: śmierć postaci która NIE jest w 'joined the area' = twoja", flush=True)

    # Use asyncio-friendly file reading via run_in_executor
    loop = asyncio.get_event_loop()
    f = open(CLIENT_TXT, "r", encoding="utf-8", errors="ignore")
    f.seek(0, 2)  # EOF — only watch new lines

    while True:
        line = await loop.run_in_executor(None, f.readline)
        if not line:
            await asyncio.sleep(0.5)
            continue

        # Track other players joining — they are NOT you
        join_m = re.search(r': (\S+) has joined the area', line)
        if join_m:
            other = join_m.group(1)
            _other_players.add(other.lower())
            continue

        # Clear others when you enter a new area (fresh instance)
        if "You have entered" in line:
            _other_players.clear()
            continue

        # Death detection
        if "has been slain" not in line:
            continue

        m = re.search(r': (\S+) has been slain', line)
        if not m:
            continue

        char_name = m.group(1)

        # Skip if this is a known other player
        if char_name.lower() in _other_players:
            continue

        # This is YOUR death — count immediately, roast async
        current_char = char_name
        _char_deaths[char_name] = _char_deaths.get(char_name, 0) + 1
        last_death_time = time.time()
        last_roast = ""  # Clear stale roast — JS will poll for it
        print(f"[Death] #{death_count()} — {char_name} has been slain!", flush=True)
        await notify_clients()

        # Roast arrives later — JS picks it up via poll
        last_roast = await generate_roast()
        print(f"[Death] Roast ready: {last_roast}", flush=True)
        print(f"[Death] Notified {len(_clients)} overlay client(s)", flush=True)


# === HTTP Overlay Server ===

OVERLAY_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: transparent;
    font-family: 'Segoe UI', sans-serif;
    overflow: hidden;
  }
  .container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    gap: 10px;
  }
  .death-counter {
    background: rgba(20, 0, 0, 0.85);
    border: 3px solid #ff3333;
    border-radius: 12px;
    padding: 20px 36px;
    color: #ff4444;
    font-size: 48px;
    font-weight: bold;
    text-shadow: 0 0 15px rgba(255, 50, 50, 0.5);
    display: flex;
    align-items: center;
    gap: 18px;
    opacity: 0;
    transform: translateY(-20px);
    transition: opacity 1.5s ease, transform 1.5s ease;
  }
  .death-counter.visible {
    opacity: 1;
    transform: translateY(0);
  }
  .death-counter.fading {
    opacity: 0;
    transform: translateY(-20px);
  }
  .skull { font-size: 54px; }
  .count { color: #ffffff; }
  .roast {
    background: rgba(0, 0, 0, 0.8);
    border: 1px solid #666;
    border-radius: 8px;
    padding: 14px 24px;
    color: #ffcc00;
    font-size: 26px;
    font-style: italic;
    max-width: 750px;
    opacity: 0;
    transform: translateY(10px);
    transition: opacity 1.5s ease, transform 1.5s ease;
  }
  .roast.visible {
    opacity: 1;
    transform: translateY(0);
  }
  .roast.fading {
    opacity: 0;
    transform: translateY(10px);
  }
</style>
</head>
<body>
<div class="container">
  <div class="death-counter" id="counter">
    <span class="skull">💀</span>
    <span>DEATHS: <span class="count" id="count">0</span></span>
  </div>
  <div class="roast" id="roast"></div>
</div>
<script>
  const counterEl = document.getElementById('counter');
  const countEl = document.getElementById('count');
  const roastEl = document.getElementById('roast');
  let hideTimeout = null;
  let lastCount = 0;
  let lastChar = '';
  let waitingForRoast = false;

  async function poll() {
    try {
      const r = await fetch('/api');
      const data = await r.json();

      // Character switch — reset tracking
      if (data.char && data.char !== lastChar) {
        lastChar = data.char;
        lastCount = 0;
      }

      if (data.count > lastCount) {
        lastCount = data.count;
        countEl.textContent = data.count;
        counterEl.className = 'death-counter visible';
        roastEl.className = 'roast';  // hide old roast
        roastEl.textContent = '';
        waitingForRoast = true;

        // Reset fade timer — will restart when roast arrives
        clearTimeout(hideTimeout);
      }

      // Roast arrived after count
      if (waitingForRoast && data.roast) {
        waitingForRoast = false;
        roastEl.textContent = data.roast;
        roastEl.className = 'roast visible';

        // Both fade out 12s after roast appears
        clearTimeout(hideTimeout);
        hideTimeout = setTimeout(() => {
          roastEl.className = 'roast fading';
          setTimeout(() => {
            counterEl.className = 'death-counter fading';
          }, 500);
        }, 12000);
      }
    } catch(e) {}
  }

  // Poll every second — lightweight, OBS-proof
  setInterval(poll, 1000);
</script>
</body>
</html>"""


async def handle_overlay(request):
    return web.Response(text=OVERLAY_HTML, content_type="text/html")


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _clients.append(ws)
    # Send current state immediately
    await ws.send_str(json.dumps({"count": death_count(), "char": current_char, "roast": last_roast, "time": last_death_time}))
    async for _ in ws:
        pass  # Keep alive, we only push
    _clients.remove(ws)
    return ws


async def handle_api(request):
    """Simple JSON API for external integrations."""
    return web.json_response({"count": death_count(), "char": current_char, "roast": last_roast, "time": last_death_time})


async def main():
    # Start web server
    app = web.Application()
    app.router.add_get("/", handle_overlay)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api", handle_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", OVERLAY_PORT)
    await site.start()
    print(f"[Death] Overlay: http://localhost:{OVERLAY_PORT}")
    print(f"[Death] API: http://localhost:{OVERLAY_PORT}/api")
    print(f"[Death] Dodaj w OBS → Browser Source → URL: http://localhost:{OVERLAY_PORT}")

    # Start watching Client.txt
    await tail_client_txt()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Death] Zamykanie...")
