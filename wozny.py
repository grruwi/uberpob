#!/usr/bin/env python3
"""wozny.py — DECYDENT: co ze zdarzeń w grze jest warte obudzenia Gieni.

Piętro między logiem a bytem. Powstał, bo żadna z dwóch prostych dróg nie działa:

  • log PCHA wszystko do Gieni  → zasypuje ją (7006 craftów, 10 077 wejść w obszar)
  • Gienia sama PYTA o zdarzenia → nie ma czym; to agent w terminalu, bez własnej pętli.
    Ktoś MUSI jej wcisnąć Enter — dokładnie tak, jak dyktafon wciska tekst Klodziowi.

Więc: woźny odpytuje `poe_events.py`, przepuszcza przez progi, i to, co przeszło, wysyła
jednym zdaniem kibelkiem. Kibelek dostaje ZACZEPKĘ („zginąłeś 4. raz na Ledge, powiedz coś"),
a nie strumień faktów — zasada „do kibelka mogą srać tylko istoty" zostaje nienaruszona.

⭐ Całe strojenie siedzi TUTAJ. Nie w logu i nie w Gieni. Chcesz, żeby rzadziej gadała —
zmieniasz liczbę w `REGULY`, nic więcej.

Uruchomienie:
    python3 wozny.py                 # normalnie
    python3 wozny.py --sucho         # pokazuje, co BY wysłał, nie wysyła nic
    python3 wozny.py --do klodzio    # inny adresat niż domyślna Gienia
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ZRODLO = "http://localhost:6970/zdarzenia"     # poe_events wystawia to na każdym z 3 portów
KIB_SEND = Path.home() / "Dokumenty/jarvis/kib-send.py"
KIB_SOCK = Path.home() / ".config/jarvis/kibelek.sock"
ODPYTUJ_CO = 5                                  # sekund

# ── PROGI ─────────────────────────────────────────────────────────────────────
# Dane, nie kod. Liczby wzięte z rozkładu w prawdziwym logu (85 MB, 2026-08-12):
# craft 7006 · obszar 10 077 · level 650 · śmierć 314 · Reflecting Mist 7 · mapa czysta 8.
#   zawsze     — każde wystąpienie budzi
#   co_n       — budzi co n-te wystąpienie (reszta przelatuje)
#   tylko_gdy  — budzi, gdy podtekst występuje w treści zdarzenia
REGULY = {
    "smierc": {"zawsze": True},                  # rzadkie i zawsze coś znaczy
    "drop":   {"zawsze": True},                  # 7 razy w całym logu — nie ma czego dławić
    "mapa":   {"zawsze": True},                  # 8 razy; u tego gracza to niemal święto
    "afk":    {"zawsze": True},                  # naturalny moment na odezwanie się
    "level":  {"co_n": 5},
    "trade":  {"co_n": 3},
    "obszar": {"co_n": 100},                     # 10 tys. wejść — bez tego gadałaby bez przerwy
    "craft":  {"tylko_gdy": "CORRUPTED"},        # 7 tys. faili, ale corrupted boli naprawdę
}

# Nawet gdy próg przepuści, byt nie może być zagadany na śmierć.
ODSTEP_MIN = 90        # sekund między dwoma obudzeniami
CISZA_PO_STARCIE = 20  # nie zaczepiaj od razu po uruchomieniu


def log(*a):
    print("[wozny]", *a, file=sys.stderr, flush=True)


def przepuscic(z: dict, licznik: Counter) -> bool:
    """Czy to zdarzenie zasługuje na obudzenie bytu."""
    r = REGULY.get(z.get("event"))
    if not r:
        return False
    if "tylko_gdy" in r:
        return r["tylko_gdy"].lower() in (z.get("text", "") + str(z.get("powod", ""))).lower()
    if "co_n" in r:
        licznik[z["event"]] += 1
        return licznik[z["event"]] % r["co_n"] == 0
    return bool(r.get("zawsze"))


def zbudz(tresc: str, adresat: str, sucho: bool):
    """Jedno zdanie do bytu. To ZACZEPKA — nie dane, nie log, nie raport."""
    if sucho:
        log(f"[sucho] -> {adresat}: {tresc}")
        return
    if not KIB_SOCK.exists():
        log("kibelek nie stoi — pomijam")
        return
    try:
        subprocess.Popen(
            ["python3", str(KIB_SEND), "--from", "poe", "--to", adresat, "--type", "zaczepka", tresc],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"-> {adresat}: {tresc}")
    except Exception as e:
        log("nie udało się wysłać:", repr(e))


def pobierz(od: float) -> tuple[float, list]:
    try:
        with urllib.request.urlopen(f"{ZRODLO}?od={od}", timeout=4) as r:
            d = json.load(r)
        return d.get("teraz", od), d.get("zdarzenia", [])
    except (urllib.error.URLError, TimeoutError, OSError):
        return od, []      # poe_events jeszcze nie stoi — cicho czekamy, to normalne przed streamem


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--do", default="gienia", help="adresat zaczepek (domyślnie: gienia)")
    p.add_argument("--sucho", action="store_true", help="tylko pokazuj, nic nie wysyłaj")
    a = p.parse_args()

    log(f"budzę '{a.do}' co najwyżej raz na {ODSTEP_MIN} s" + (" [SUCHY BIEG]" if a.sucho else ""))
    log(f"progi: {json.dumps(REGULY, ensure_ascii=False)}")

    od = time.time()
    ostatnie_zbudzenie = time.time() - ODSTEP_MIN + CISZA_PO_STARCIE
    licznik = Counter()

    while True:
        time.sleep(ODPYTUJ_CO)
        od, zdarzenia = pobierz(od)
        for z in zdarzenia:
            if not przepuscic(z, licznik):
                continue
            # Hamulec sprawdzamy PO progach — inaczej zdarzenie odrzucone przez ciszę
            # zjadałoby swoją kolejkę w `co_n` i licznik rozjeżdżałby się z rzeczywistością.
            if time.time() - ostatnie_zbudzenie < ODSTEP_MIN:
                log(f"(cisza) pomijam: {z['text'][:60]}")
                continue
            zbudz(z["text"], a.do, a.sucho)
            ostatnie_zbudzenie = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("pa pa")
