
import httpx
import os
import re
import time
import yaml
from pathlib import Path
import asyncio

# === Cache i Stałe ===
_NINJA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://poe.ninja/poe1/builds",
    "Accept": "application/json",
}
# Mapowanie user-friendly → URL slug (PoE1)
NINJA_LEAGUES = {
    "sc": "mirage", "mirage": "mirage",
    "hc": "miragehc", "hardcore": "miragehc",
    "ssf": "miragessf",
    "hcssf": "miragehcssf",
    "ruthless": "mirager",
}
# Mapowanie user-friendly → URL slug (PoE2)
POE2_NINJA_LEAGUES = {
    "sc": "vaal", "vaal": "vaal",
    "hc": "vaalhc", "hardcore": "vaalhc",
    "ssf": "vaalssf",
    "hcssf": "vaalhcssf",
    "standard": "standard",
}

_ECONOMY_CACHE: dict[str, tuple[list, float]] = {}
_ECONOMY_TTL = 1800  # 30 min
_ECONOMY_ITEM_TYPES = [
    "UniqueWeapon", "UniqueArmour", "UniqueAccessory",
    "UniqueJewel", "UniqueFlask", "SkillGem",
    "DivinationCard", "Scarab", "Fragment",
]

# Baza verified buildów
_DB_PATH = Path(__file__).parent / "builds_db.yaml"
with open(_DB_PATH) as f:
    BUILDS_DB: dict = yaml.safe_load(f).get("archetypes", {})

# === Narzędzia ===

async def _fetch_economy_type(client: httpx.AsyncClient, type_name: str, league: str) -> list:
    cache_key = f"{type_name}:{league}"
    now = time.time()
    if cache_key in _ECONOMY_CACHE:
        data, ts = _ECONOMY_CACHE[cache_key]
        if now - ts < _ECONOMY_TTL:
            return data
    if type_name == "Currency":
        url = f"https://poe.ninja/api/data/currencyoverview?league={league}&type=Currency"
    else:
        url = f"https://poe.ninja/api/data/itemoverview?league={league}&type={type_name}"
    try:
        r = await client.get(url, headers=_NINJA_HEADERS)
        if r.status_code == 200:
            data = r.json().get("lines", [])
            _ECONOMY_CACHE[cache_key] = (data, now)
            return data
    except Exception as e:
        print(f"[economy] błąd {type_name}: {e}")
    return []


async def check_ninja_price(item_name: str, league: str = "Mirage") -> str:
    item_lower = item_name.lower()
    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for entry in await _fetch_economy_type(client, "Currency", league):
            name = entry.get("currencyTypeName", "")
            if item_lower in name.lower():
                chaos = entry.get("chaosEquivalent", 0)
                results.append(f"{name}: {chaos:.1f}c")
        for type_name in _ECONOMY_ITEM_TYPES:
            for entry in await _fetch_economy_type(client, type_name, league):
                name = entry.get("name", "")
                if item_lower in name.lower():
                    chaos = entry.get("chaosValue", 0)
                    divine = entry.get("divineValue", 0)
                    count = entry.get("count", 0)
                    price_str = f"{chaos:.0f}c"
                    if chaos > 50:
                        price_str += f" (~{divine:.2f} div)"
                    results.append(f"{name}: {price_str} ({count} listings)")
    if not results:
        return f"Nie znaleziono '{item_name}' na poe.ninja economy (liga: {league})."
    return f"Ceny z poe.ninja [{league}]:\n" + "\n".join(results[:10])


async def search_trade_listings(item_name: str, league: str = "Mirage") -> str:
    session_id = os.getenv("POESESSID", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_id:
        headers["Cookie"] = f"POESESSID={session_id}"
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        try:
            r = await client.post(
                f"https://www.pathofexile.com/api/trade/search/{league}",
                json={"query": {"name": item_name, "status": {"option": "online"}}, "sort": {"price": "asc"}},
            )
            if r.status_code != 200:
                return f"Błąd trade search: HTTP {r.status_code}. Dodaj POESESSID do .env dla lepszego rate limit."
            data = r.json()
            result_ids = data.get("result", [])
            query_id = data.get("id", "")
            total = data.get("total", 0)
            if not result_ids:
                return f"Brak ofert dla '{item_name}' na trade (liga: {league})."
            r2 = await client.get(
                f"https://www.pathofexile.com/api/trade/fetch/{','.join(result_ids[:5])}",
                params={"query": query_id},
            )
            if r2.status_code != 200:
                return f"Błąd trade fetch: HTTP {r2.status_code}"
            listings = r2.json().get("result", [])
            lines = [f"Trade: {item_name} [{league}] | {total} ofert online, top {len(listings)}:"]
            for ld in listings:
                listing = ld.get("listing", {})
                price = listing.get("price", {})
                amount = price.get("amount", "?")
                currency = price.get("currency", "?")
                account = listing.get("account", {}).get("name", "?")
                whisper = listing.get("whisper", "")
                lines.append(f"  {account}: {amount} {currency}")
            return "\n".join(lines)
        except Exception as e:
            return f"Błąd trade: {e}"


async def search_poe_wiki(query: str) -> str:
    """Szuka mechaniki/itemu/keystone na poewiki.net i zwraca skrócony tekst artykułu."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            # Szukaj pasujących stron
            r = await client.get(
                "https://www.poewiki.net/w/api.php",
                params={"action": "query", "list": "search", "srsearch": query,
                        "srlimit": 3, "format": "json"},
            )
            if r.status_code != 200:
                return f"Wiki niedostępna (HTTP {r.status_code})"
            results = r.json().get("query", {}).get("search", [])
            if not results:
                return f"Nie znaleziono '{query}' na poewiki.net"

            # Pobierz treść pierwszej strony (wikitext → plaintext przez extracts)
            title = results[0]["title"]
            r2 = await client.get(
                "https://www.poewiki.net/w/api.php",
                params={"action": "query", "titles": title, "prop": "extracts",
                        "exintro": True, "explaintext": True, "exsectionformat": "plain",
                        "format": "json"},
            )
            if r2.status_code != 200:
                return f"Błąd pobierania artykułu (HTTP {r2.status_code})"
            pages = r2.json().get("query", {}).get("pages", {})
            page = next(iter(pages.values()))
            extract = page.get("extract", "").strip()
            if not extract:
                return f"Artykuł '{title}' istnieje ale jest pusty."
            # Utnij do ~1500 znaków żeby nie bomb kontekstu
            if len(extract) > 1500:
                extract = extract[:1500] + "... [skrócono]"
            return f"[poewiki: {title}]\n{extract}"
        except Exception as e:
            return f"Błąd wiki: {e}"

def format_full_archetype(name: str, data: dict) -> str:
    """Formatuje pełne dane archetype do wstrzyknięcia na żądanie."""
    newcomer = " [POLECANY DLA NOWYCH GRACZY]" if data.get("newcomer_friendly") else ""
    lines = [f"=== ARCHETYPE: {name} [Tier {data.get('tier', '?')}]{newcomer} ==="]
    pobb = data.get("pobb_in") or data.get("maxroll", "")
    if pobb:
        lines.append(f"Referencyjna PoB: {pobb}")
    benchmarks = data.get("benchmarks", {})
    if benchmarks:
        lines.append("Benchmarki (min / cel):")
        for stat, bench in benchmarks.items():
            min_v = bench.get("min", "")
            good_v = bench.get("good", "")
            note = bench.get("note", "")
            unit = bench.get("unit", "")
            line = f"  {stat}: min {min_v}{unit} / cel {good_v}{unit}"
            if note:
                line += f" — {note}"
            lines.append(line)
    notes = data.get("notes", [])
    if notes:
        lines.append("Wskazówki:")
        for n in notes:
            lines.append(f"  • {n}")
    lines.append("=" * 40)
    return "\n".join(lines)


def lookup_build_data(archetype_name: str) -> str:
    """Fuzzy search archetype w bazie, zwraca pełne dane."""
    name_lower = archetype_name.lower()
    # exact match first
    for name, data in BUILDS_DB.items():
        if name.lower() == name_lower:
            return format_full_archetype(name, data)
    # substring match
    for name, data in BUILDS_DB.items():
        if name_lower in name.lower() or name.lower() in name_lower:
            return format_full_archetype(name, data)
    # word match — any word from query hits name
    words = name_lower.split()
    matches = [(name, data) for name, data in BUILDS_DB.items()
               if sum(1 for w in words if w in name.lower()) >= max(1, len(words) - 1)]
    if matches:
        name, data = matches[0]
        return format_full_archetype(name, data)
    available = ", ".join(BUILDS_DB.keys())
    return f"Nie znaleziono '{archetype_name}' w bazie. Dostępne: {available}"


async def dispatch_tool(name: str, args: dict) -> str:
    league = args.get("league", "Mirage")
    if name == "lookup_build":
        return lookup_build_data(args.get("archetype_name", ""))
    if name == "check_ninja_price":
        return await check_ninja_price(args.get("item_name", ""), league)
    if name == "search_trade_listings":
        return await search_trade_listings(args.get("item_name", ""), league)
    if name == "search_poe_wiki":
        return await search_poe_wiki(args.get("query", ""))
    return f"Nieznane narzędzie: {name}"

