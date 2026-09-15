"""Gnomik — animated stream companion overlay. Pepe-gnome that reacts to PoE and voice.

Monitors Client.txt for events and microphone amplitude via Web Audio API.
Serves overlay on http://localhost:6971 — add as OBS Browser Source.
"""

import os
import re
import asyncio
import json
import time
from pathlib import Path
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

CLIENT_TXT = Path(os.getenv(
    "POE_CLIENT_TXT",
    os.path.expanduser("~/.local/share/Steam/steamapps/common/Path of Exile/logs/Client.txt")
))
OVERLAY_PORT = int(os.getenv("GNOMIK_PORT", "6971"))

# State
_state = {
    "mood": "idle",
    "mood_since": 0,
    "deaths": 0,
    "level_ups": 0,
    "current_area": "",
    "afk": False,
    "last_event": "",
    "streak_alive": 0,
    "last_death": 0,
}
_other_players: set[str] = set()


def set_mood(mood: str, event: str = ""):
    _state["mood"] = mood
    _state["mood_since"] = time.time()
    _state["last_event"] = event
    print(f"[Gnomik] {mood} — {event}", flush=True)


async def tail_client_txt():
    """Watch Client.txt and update gnomik state based on events."""
    if not CLIENT_TXT.exists():
        print(f"[Gnomik] Client.txt nie znaleziony: {CLIENT_TXT}", flush=True)
        return

    print(f"[Gnomik] Monitoring {CLIENT_TXT}", flush=True)

    loop = asyncio.get_event_loop()
    f = open(CLIENT_TXT, "r", encoding="utf-8", errors="ignore")
    f.seek(0, 2)

    while True:
        line = await loop.run_in_executor(None, f.readline)
        if not line:
            await asyncio.sleep(0.5)
            if _state["mood"] not in ("idle",) and time.time() - _state["mood_since"] > 15:
                set_mood("idle", "")
            continue

        # Other players joining
        join_m = re.search(r': (\S+) has joined the area', line)
        if join_m:
            _other_players.add(join_m.group(1).lower())
            continue

        # New area
        area_m = re.search(r': You have entered (.+)\.', line)
        if area_m:
            _other_players.clear()
            _state["current_area"] = area_m.group(1)
            set_mood("looking", f"→ {area_m.group(1)}")
            continue

        # Death
        death_m = re.search(r': (\S+) has been slain', line)
        if death_m and death_m.group(1).lower() not in _other_players:
            _state["deaths"] += 1
            _state["last_death"] = time.time()
            _state["streak_alive"] = 0
            set_mood("facepalm", f"RIP #{_state['deaths']}")
            continue

        # Level up
        if "is now level" in line:
            level_m = re.search(r': (\S+) \(.+\) is now level (\d+)', line)
            if level_m and level_m.group(1).lower() not in _other_players:
                _state["level_ups"] += 1
                lvl = level_m.group(2)
                set_mood("dance", f"Level {lvl}!")
                continue

        # AFK
        if "AFK mode is now ON" in line:
            _state["afk"] = True
            set_mood("sleeping", "AFK")
            continue
        if "AFK mode is now OFF" in line:
            _state["afk"] = False
            set_mood("happy", "obudzony!")
            continue

        # Craft fail
        if "Failed to apply item:" in line:
            reason = line.split("Failed to apply item:")[-1].strip()
            if "corrupted" in reason.lower():
                set_mood("panic", "CORRUPTED!")
            else:
                set_mood("sad", "craft fail")
            continue

        # Map cleared
        if "0 monsters remain" in line:
            set_mood("clap", "mapa czysta!")
            continue

        # Reflecting Mist
        if "Reflecting Mist" in line:
            set_mood("happy", "✨ Reflecting Mist!")
            continue

        # Trade fail
        if "Another player has secured" in line:
            set_mood("sad", "ktoś sprzątnął item")
            continue


# === OVERLAY HTML ===

OVERLAY_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: transparent; overflow: hidden; }

  .gnomik-container {
    width: 100vw;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }

  /* Speech bubble */
  .bubble {
    background: rgba(0, 0, 0, 0.8);
    border: 1px solid #888;
    border-radius: 12px;
    padding: 6px 12px;
    color: #fff;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
    max-width: 200px;
    text-align: center;
    opacity: 0;
    transform: translateY(5px);
    transition: opacity 0.5s ease, transform 0.5s ease;
  }
  .bubble.visible {
    opacity: 1;
    transform: translateY(0);
  }

  /* Gnomik image container */
  .gnomik-wrap {
    position: relative;
    width: 128px;
    height: 128px;
  }

  .gnomik {
    width: 100%;
    height: 100%;
    transition: transform 0.3s ease;
    image-rendering: pixelated;
  }
  .gnomik img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    pointer-events: none;
  }

  /* === MOOD ANIMATIONS === */
  .gnomik.idle { animation: idle-bob 3s ease-in-out infinite; }
  .gnomik.sleeping { animation: sleep-bob 4s ease-in-out infinite; opacity: 0.7; }
  .gnomik.happy { animation: happy-bounce 0.5s ease infinite; }
  .gnomik.facepalm { animation: facepalm-shake 0.3s ease 5; }
  .gnomik.panic { animation: panic-shake 0.15s ease 8; }
  .gnomik.clap { animation: happy-bounce 0.3s ease 5; }
  .gnomik.looking { animation: look-tilt 1.2s ease 2; }
  .gnomik.sad { animation: sad-droop 2s ease-in-out infinite; }
  .gnomik.dance { animation: dance-wiggle 0.4s ease infinite; }

  /* Voice-reactive class — applied dynamically via JS */
  .gnomik.voice-bump {
    /* overridden by JS inline transform */
  }
  .gnomik.voice-shout {
    animation: shout-react 0.3s ease;
    filter: brightness(1.3) saturate(1.4);
  }

  @keyframes idle-bob {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-4px); }
  }
  @keyframes sleep-bob {
    0%, 100% { transform: translateY(0) rotate(0deg); }
    50% { transform: translateY(3px) rotate(-8deg); }
  }
  @keyframes happy-bounce {
    0%, 100% { transform: translateY(0) scale(1); }
    50% { transform: translateY(-12px) scale(1.05); }
  }
  @keyframes facepalm-shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-6px) rotate(-3deg); }
    75% { transform: translateX(6px) rotate(3deg); }
  }
  @keyframes panic-shake {
    0%, 100% { transform: translateX(0) rotate(0); }
    25% { transform: translateX(-10px) rotate(-12deg); }
    75% { transform: translateX(10px) rotate(12deg); }
  }
  @keyframes look-tilt {
    0%, 100% { transform: rotate(0deg); }
    30% { transform: rotate(10deg) translateX(5px); }
    70% { transform: rotate(-8deg) translateX(-3px); }
  }
  @keyframes sad-droop {
    0%, 100% { transform: translateY(0) rotate(0deg) scale(1); }
    50% { transform: translateY(4px) rotate(-4deg) scale(0.95); }
  }
  @keyframes dance-wiggle {
    0%, 100% { transform: rotate(0deg) translateY(0); }
    25% { transform: rotate(12deg) translateY(-8px); }
    75% { transform: rotate(-12deg) translateY(-8px); }
  }
  @keyframes shout-react {
    0% { transform: scale(1); }
    30% { transform: scale(1.2) translateY(-8px); }
    100% { transform: scale(1); }
  }

  /* Sleeping Zzz */
  .zzz {
    position: absolute;
    top: -10px;
    right: 5px;
    font-size: 20px;
    opacity: 0;
    animation: none;
  }
  .sleeping .zzz {
    animation: zzz-float 2s ease-in-out infinite;
    opacity: 1;
  }
  @keyframes zzz-float {
    0% { transform: translateY(0); opacity: 0.3; }
    50% { transform: translateY(-18px); opacity: 1; }
    100% { transform: translateY(-30px); opacity: 0; }
  }

  /* Hat tilt for sleeping */
  .gnomik.sleeping img {
    transform: rotate(-8deg);
    transform-origin: center top;
    transition: transform 0.5s ease;
  }

  /* Voice level indicator (debug, hidden by default) */
  .voice-debug {
    position: fixed;
    bottom: 5px;
    left: 5px;
    font-size: 10px;
    color: rgba(255,255,255,0.3);
    font-family: monospace;
    display: none; /* set to block for debug */
  }
</style>
</head>
<body>
<div class="gnomik-container">
  <div class="bubble" id="bubble"></div>
  <div class="gnomik-wrap" id="wrap">
    <div class="gnomik idle" id="gnomik">
      <img src="/gnomik.png" alt="gnomik">
    </div>
    <span class="zzz" id="zzz">💤</span>
  </div>
</div>
<div class="voice-debug" id="voiceDebug">vol: 0</div>

<script>
  const gnomikEl = document.getElementById('gnomik');
  const bubbleEl = document.getElementById('bubble');
  const wrapEl = document.getElementById('wrap');
  const voiceDebug = document.getElementById('voiceDebug');
  let lastMood = '';
  let lastEvent = '';
  let bubbleTimeout = null;
  let currentMood = 'idle';

  const moodEmojis = {
    idle: '',
    sleeping: '',
    happy: '✨',
    facepalm: '💀',
    panic: '😱',
    clap: '👏',
    looking: '👀',
    sad: '😔',
    dance: '🎉',
  };

  // === POE EVENT POLLING ===
  async function poll() {
    try {
      const r = await fetch('/api');
      const data = await r.json();

      if (data.mood !== lastMood || data.last_event !== lastEvent) {
        lastMood = data.mood;
        lastEvent = data.last_event;
        currentMood = data.mood;

        gnomikEl.className = 'gnomik ' + data.mood;

        // Zzz
        if (data.mood === 'sleeping') {
          wrapEl.classList.add('sleeping');
        } else {
          wrapEl.classList.remove('sleeping');
        }

        // Bubble
        if (data.mood !== 'idle' && data.last_event) {
          const emoji = moodEmojis[data.mood] || '';
          bubbleEl.textContent = emoji + ' ' + data.last_event;
          bubbleEl.className = 'bubble visible';
          clearTimeout(bubbleTimeout);
          bubbleTimeout = setTimeout(() => {
            bubbleEl.className = 'bubble';
          }, 8000);
        }
      }
    } catch(e) {}
  }
  setInterval(poll, 1000);

</script>
</body>
</html>"""


async def handle_overlay(request):
    return web.Response(text=OVERLAY_HTML, content_type="text/html")


async def handle_api(request):
    if _state["last_death"] > 0:
        _state["streak_alive"] = int(time.time() - _state["last_death"])
    return web.json_response(_state)


async def handle_png(request):
    png_path = Path(__file__).parent / "gnomik.png"
    return web.FileResponse(png_path)


async def main():
    app = web.Application()
    app.router.add_get("/", handle_overlay)
    app.router.add_get("/api", handle_api)
    app.router.add_get("/gnomik.png", handle_png)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", OVERLAY_PORT)
    await site.start()
    print(f"[Gnomik] Overlay: http://localhost:{OVERLAY_PORT}", flush=True)
    print(f"[Gnomik] OBS → Browser Source → URL: http://localhost:{OVERLAY_PORT}", flush=True)
    print(f"[Gnomik] Rozmiar: 200x200, przezroczyste tło", flush=True)

    await tail_client_txt()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[Gnomik] Pa pa!")
