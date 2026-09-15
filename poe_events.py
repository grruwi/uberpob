#!/usr/bin/env python3
"""poe_events.py — JEDEN czytelnik Client.txt dla całego streamu.

Zastępuje trzy osobne procesy (`death_counter.py`, `kawka.py`, `gnomik.py`), z których
każdy tailował TEN SAM plik i trzymał własną, prywatną kopię stanu. Skutek starego układu:
licznik śmierci, kawka i gnomik potrafiły się rozjechać, a bot czatu nie miał do żadnego
z nich dojścia — więc odpowiadał na wiadomości nie wiedząc, że gracz właśnie zginął.

Co robi ten proces:
  1. czyta Client.txt RAZ i trzyma jeden wspólny stan
  2. serwuje te same trzy overlaye pod tymi samymi portami i tymi samymi trasami,
     więc **w OBS nie trzeba zmieniać ani jednego URL-a**
  3. wystawia zdarzenia pod `/zdarzenia` — kto ich potrzebuje, ten po nie sięga

⭐ Gienia NIE dostaje dostępu do Client.txt i nie dostanie. Log gry niesie szepty od graczy,
nazwy kont i ceny z handlu — model nie musi tego widzieć, a kontener nie musi widzieć dysku.
Sięga po wąskie, gotowe zdarzenie: „śmierć, postać X, obszar Y".

⛔ I nie idzie to KIBELKIEM. Kibelek jest kanałem rozmowy między bytami — wpycha treść wprost
w terminale i ma feed dla ludzkich oczu. Strumień zdarzeń z gry (10 tys. wejść w obszar
na sesję) zasypałby i jedno, i drugie. Fakty czekają tutaj; kibelkiem odzywa się dopiero
Gienia, gdy ma coś do powiedzenia.

HTML overlayów NIE jest tu kopiowany — wyciągamy go z oryginalnych plików przez AST,
bez importu (import odpalałby ich `load_dotenv()` i tworzenie klienta Gemini).
Oryginały zostają nietknięte i dalej działają samodzielnie, jako awaryjne wyjście.

Uruchomienie:
    ~/Dokumenty/uberpob/.venv/bin/python poe_events.py
"""

import ast
import asyncio
import json
import os
import re
import time
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

KATALOG = Path(__file__).parent

# ⚠️ Ścieżka zaszyta w starych overlayach (`~/.local/share/Steam/...`) NIE ISTNIEJE na tej
# maszynie — biblioteka Steam leży na osobnej partycji `/games`. Bez `POE_CLIENT_TXT` w `.env`
# tamte procesy wypisywały „Client.txt nie znaleziony" i po prostu nie robiły NIC.
# Dlatego tutaj: najpierw zmienna z konfiguracji, a gdy jej brak — szukamy po znanych miejscach.
_KANDYDACI = [
    "/games/SteamLibrary/steamapps/common/Path of Exile/logs/Client.txt",
    os.path.expanduser("~/.local/share/Steam/steamapps/common/Path of Exile/logs/Client.txt"),
    os.path.expanduser("~/.steam/steam/steamapps/common/Path of Exile/logs/Client.txt"),
    "/games/SteamLibrary/steamapps/common/Path of Exile 2/logs/Client.txt",
]


def _znajdz_log() -> Path:
    z_konfigu = os.getenv("POE_CLIENT_TXT")
    if z_konfigu:
        return Path(z_konfigu)
    for k in _KANDYDACI:
        if Path(k).exists():
            return Path(k)
    return Path(_KANDYDACI[0])


CLIENT_TXT = _znajdz_log()
PORT_DEATH = int(os.getenv("DEATH_OVERLAY_PORT", "6969"))
PORT_KAWKA = int(os.getenv("KAWKA_PORT", "6970"))
PORT_GNOMIK = int(os.getenv("GNOMIK_PORT", "6971"))

# ⛔ NIC STĄD NIE IDZIE DO KIBELKA (grruwi 2026-08-12: „do kibelka mogą srać tylko istoty,
# nie sraj mi tam streamem logów"). Kibelek niesie WYPOWIEDZI bytów, nie strumień faktów —
# ma ludzki feed i wpycha treść wprost do terminali, więc 10 tysięcy wejść w obszar zasypałoby
# i okienko, i czyjś prompt. Zdarzenia z gry zostają tutaj i czeka się po nie pod `/api`.
# Gdy coś jest naprawdę warte słowa, do kibelka odzywa się GIENIA — od siebie, jednym zdaniem.
# Fakty czekają pod `/api` (stan) i `/zdarzenia` (co się wydarzyło od czasu X).
DZIENNIK = Path("/tmp/poe-events.jsonl")   # ostatnie zdarzenia, do podejrzenia i do pull-a

# ── STAN — jeden, wspólny ────────────────────────────────────────────────────
_stan = {
    "mood": "idle",          # idle sleeping happy facepalm panic clap looking sad dance
    "mood_since": 0,
    "deaths": 0,
    "level_ups": 0,
    "current_area": "",
    "afk": False,
    "last_event": "",
    "streak_alive": 0,
    "last_death": 0,
}
_deaths_per_char: dict[str, int] = {}
_current_char = ""
_last_roast = ""
_inni_gracze: set[str] = set()   # ci, którzy "joined the area" — czyli NIE ty


def log(*a):
    print("[poe-events]", *a, flush=True)


def ustaw_nastroj(mood: str, event: str = ""):
    _stan["mood"] = mood
    _stan["mood_since"] = time.time()
    _stan["last_event"] = event
    log(f"{mood} — {event}")


# ── DZIENNIK ZDARZEŃ — do odpytania, nie do wypychania ───────────────────────
_ostatnie: list[dict] = []          # pamięć podręczna dla `/zdarzenia`
_LIMIT = 200                        # tyle wystarczy; log gry potrafi 10 tys. wpisów na sesję


def zdarzenie(rodzaj: str, tresc: str, dane: dict):
    """Zapisuje zdarzenie u siebie. Nikogo nie zaczepia — zainteresowany pyta sam.

    Push odpada świadomie: odbiorca (Gienia, bot, overlay) ma sięgnąć po to, czego akurat
    potrzebuje, zamiast dostawać wszystko, co się w grze rusza."""
    wpis = {"t": time.time(), "event": rodzaj, "text": tresc, **dane}
    _ostatnie.append(wpis)
    del _ostatnie[:-_LIMIT]
    try:
        with open(DZIENNIK, "a") as f:
            f.write(json.dumps(wpis, ensure_ascii=False) + "\n")
    except Exception as e:
        log("dziennik nie przyjął:", repr(e))


# ── ROAST (przeniesiony z death_counter) ─────────────────────────────────────
ROAST_PROMPT = """Jesteś komentarzem sportowym Path of Exile. Gracz właśnie umarł.
Wygeneruj JEDEN krótki, sarkastyczny komentarz po polsku (max 15 słów).
Styl: komentator sportowy, trochę beka, trochę dramatyzm. Nie powtarzaj się.
Przykłady tonów: "TO JEST KATASTROFA", "szanowni państwo, to był samobójczy misklick",
"i oto nasze nadzieje olimpijskie gasną pod bosem", "RIP bozo xD"
NIE pisz nic poza samym komentarzem — zero cudzysłowów, zero wyjaśnień."""

_gemini = None


def _roast_sync() -> str:
    """Synchronicznie, w wątku — klient Gemini i pętla aiohttp nie lubią się w jednym."""
    global _gemini
    try:
        if _gemini is None:
            from google import genai
            _gemini = genai.Client(
                vertexai=True,
                project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
        from google.genai import types
        r = _gemini.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[types.Content(role="user", parts=[types.Part(text="Gracz umarł. Skomentuj.")])],
            config=types.GenerateContentConfig(system_instruction=ROAST_PROMPT, temperature=1.5),
        )
        return (r.text or "RIP").strip().strip("\"'")
    except Exception as e:
        log("roast padł:", repr(e))
        return "Technicznie żyje, praktycznie nie."


async def generuj_roast() -> str:
    return await asyncio.get_event_loop().run_in_executor(None, _roast_sync)


async def _dogeneruj_roast():
    """Dokłada roast do stanu, gdy Gemini już odpowie. JS w overlayu i tak dopytuje
    co sekundę — licznik pokazuje się od razu, tekst dochodzi chwilę później."""
    global _last_roast
    _last_roast = await generuj_roast()
    log(f"roast: {_last_roast}")


# ── TAILER — jedyny czytelnik logu ───────────────────────────────────────────
async def tail_client_txt():
    global _current_char, _last_roast

    if not CLIENT_TXT.exists():
        log(f"Client.txt nie znaleziony: {CLIENT_TXT}")
        log("Ustaw POE_CLIENT_TXT w .env albo odpal grę — reszta serwera i tak stoi.")
        return

    log(f"czytam {CLIENT_TXT} (od końca — tylko nowe linie)")
    log(f"zdarzenia trafiają do /zdarzenia i do {DZIENNIK}")

    petla = asyncio.get_event_loop()
    f = open(CLIENT_TXT, "r", encoding="utf-8", errors="ignore")
    f.seek(0, 2)

    while True:
        linia = await petla.run_in_executor(None, f.readline)
        if not linia:
            await asyncio.sleep(0.5)
            # Powrót do spoczynku po 15 s ciszy — jak w kawce, żeby mordka nie zastygała
            if _stan["mood"] not in ("idle", "sleeping") and time.time() - _stan["mood_since"] > 15:
                ustaw_nastroj("sleeping" if _stan["afk"] else "idle", "zzz" if _stan["afk"] else "")
            continue

        # Inni gracze w obszarze — to NIE ty, ich śmierci nie liczymy
        m = re.search(r": (\S+) has joined the area", linia)
        if m:
            _inni_gracze.add(m.group(1).lower())
            continue

        # Nowy obszar = świeża instancja, lista obcych się zeruje
        m = re.search(r": You have entered (.+)\.", linia)
        if m:
            _inni_gracze.clear()
            _stan["current_area"] = m.group(1)
            ustaw_nastroj("looking", f"→ {m.group(1)}")
            zdarzenie("obszar", f"wszedłeś w: {m.group(1)}", {"area": m.group(1)})
            continue

        # ŚMIERĆ
        m = re.search(r": (\S+) has been slain", linia)
        if m and m.group(1).lower() not in _inni_gracze:
            postac = m.group(1)
            _current_char = postac
            _deaths_per_char[postac] = _deaths_per_char.get(postac, 0) + 1
            _stan["deaths"] = _deaths_per_char[postac]
            _stan["last_death"] = time.time()
            _stan["streak_alive"] = 0
            _last_roast = ""          # stary roast znika, JS dopyta o nowy
            ustaw_nastroj("facepalm", f"RIP #{_stan['deaths']}")
            zdarzenie("smierc", f"zginąłeś ({postac}), śmierć #{_stan['deaths']}, obszar: {_stan['current_area']}",
                       {"char": postac, "deaths": _stan["deaths"], "area": _stan["current_area"]})
            # ⚠️ Roast leci OBOK pętli, nie w niej. Gemini odpowiada kilkanaście sekund, a tu
            # jeden tailer obsługuje wszystkie trzy overlaye naraz — `await` w tym miejscu
            # zamrażał kawkę, gnomika i most do kibelka na czas generowania. W starym układzie
            # tego nie było widać, bo licznik śmierci był osobnym procesem i blokował sam siebie.
            # (zmierzone: level-up wszedł 13 s po fakcie)
            asyncio.create_task(_dogeneruj_roast())
            continue

        # LEVEL UP
        if "is now level" in linia:
            m = re.search(r": (\S+) \(.+\) is now level (\d+)", linia)
            if m and m.group(1).lower() not in _inni_gracze:
                _stan["level_ups"] += 1
                lvl = m.group(2)
                ustaw_nastroj("dance", f"Level {lvl}!")
                zdarzenie("level", f"level {lvl}", {"char": m.group(1), "level": int(lvl)})
            continue

        # AFK
        if "AFK mode is now ON" in linia:
            _stan["afk"] = True
            ustaw_nastroj("sleeping", "AFK")
            zdarzenie("afk", "odszedłeś od kompa (AFK on)", {"afk": True})
            continue
        if "AFK mode is now OFF" in linia:
            _stan["afk"] = False
            ustaw_nastroj("happy", "obudzona!")
            zdarzenie("afk", "wróciłeś (AFK off)", {"afk": False})
            continue

        # CRAFT
        if "Failed to apply item:" in linia:
            powod = linia.split("Failed to apply item:")[-1].strip()
            if "corrupted" in powod.lower():
                ustaw_nastroj("panic", "CORRUPTED!")
                zdarzenie("craft", "item CORRUPTED przy crafcie", {"powod": powod})
            else:
                ustaw_nastroj("sad", "craft fail")
                zdarzenie("craft", f"craft nie wszedł: {powod}", {"powod": powod})
            continue

        if "0 monsters remain" in linia:
            ustaw_nastroj("clap", "mapa czysta!")
            zdarzenie("mapa", "mapa wyczyszczona", {})
            continue

        if "Reflecting Mist" in linia:
            ustaw_nastroj("happy", "✨ Reflecting Mist!")
            zdarzenie("drop", "Reflecting Mist!", {})
            continue

        if "Another player has secured" in linia:
            ustaw_nastroj("sad", "ktoś sprzątnął item")
            zdarzenie("trade", "ktoś sprzątnął item sprzed nosa", {})
            continue


# ── OVERLAYE — ten sam HTML, te same porty, te same trasy ────────────────────
def wczytaj_html(plik: str, stala: str = "OVERLAY_HTML") -> str:
    """Wyciąga stałą z pliku .py przez AST — BEZ importu.

    Import odpalałby `load_dotenv()` i tworzenie klienta Gemini na poziomie modułu,
    czyli efekty uboczne za samo sięgnięcie po kawałek HTML-a."""
    drzewo = ast.parse((KATALOG / plik).read_text(encoding="utf-8"))
    for wezel in drzewo.body:
        if isinstance(wezel, ast.Assign):
            for cel in wezel.targets:
                if isinstance(cel, ast.Name) and cel.id == stala:
                    return ast.literal_eval(wezel.value)
    raise RuntimeError(f"nie znalazłem {stala} w {plik}")


def stan_kawki() -> dict:
    s = dict(_stan)
    if s["last_death"]:
        s["streak_alive"] = int(time.time() - s["last_death"])
    return s


def stan_licznika() -> dict:
    return {"count": _deaths_per_char.get(_current_char, 0), "char": _current_char,
            "roast": _last_roast, "time": _stan["last_death"]}


def _zdarzenia(request) -> web.Response:
    """`/zdarzenia?od=<timestamp>&typ=smierc,level` — dla tego, kto sięga po fakty.

    `od` pozwala odebrać tylko to, co przyszło po ostatnim pytaniu, więc pytający
    nie musi za każdym razem przetwarzać całej listy."""
    od = float(request.query.get("od", 0) or 0)
    typy = {t for t in (request.query.get("typ") or "").split(",") if t}
    wynik = [z for z in _ostatnie if z["t"] > od and (not typy or z["event"] in typy)]
    return web.json_response({"teraz": time.time(), "zdarzenia": wynik})


async def wystaw(port: int, html: str, api) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=html, content_type="text/html"))
    app.router.add_get("/api", lambda r: web.json_response(api()))
    app.router.add_get("/zdarzenia", _zdarzenia)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", port).start()
    log(f"overlay na http://localhost:{port}")
    return runner


async def main():
    await wystaw(PORT_DEATH, wczytaj_html("death_counter.py"), stan_licznika)
    await wystaw(PORT_KAWKA, wczytaj_html("kawka.py"), stan_kawki)
    await wystaw(PORT_GNOMIK, wczytaj_html("gnomik.py"), stan_kawki)
    log("w OBS nic nie zmieniasz — te same adresy co dotąd")
    await tail_client_txt()
    # Gdy logu nie ma, tailer wraca od razu — serwery mają stać dalej, żeby overlaye
    # nie pokazywały w OBS-ie błędu połączenia tylko dlatego, że gra jeszcze nie wstała.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("pa pa")
