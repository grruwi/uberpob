import os
import re
import json
import base64
import zlib
import time
import xml.etree.ElementTree as ET
import httpx
import discord
import yaml
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Cache: pobb.in URL → raw PoB code (in-memory, żyje do restartu bota)
_pobb_cache: dict[str, str] = {}

# poe.ninja
_NINJA_DATA_DIR = Path(__file__).parent / "ninja_data"
_NINJA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://poe.ninja/poe1/builds",
    "Accept": "application/json",
}
# Mapowanie user-friendly → URL slug
NINJA_LEAGUES = {
    "sc": "mirage", "mirage": "mirage",
    "hc": "miragehc", "hardcore": "miragehc",
    "ssf": "miragessf",
    "hcssf": "miragehcssf",
    "ruthless": "mirager",
}

# === ECONOMY ===
_ECONOMY_CACHE: dict[str, tuple[list, float]] = {}
_ECONOMY_TTL = 1800  # 30 min
_ECONOMY_ITEM_TYPES = [
    "UniqueWeapon", "UniqueArmour", "UniqueAccessory",
    "UniqueJewel", "UniqueFlask", "SkillGem",
    "DivinationCard", "Scarab", "Fragment",
]


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


_POE_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="lookup_build",
        description=(
            "Pobierz szczegółowe dane archetype buildu z bazy: benchmarki statów, wskazówki, mechaniki, referencyjną PoB. "
            "Używaj gdy gracz pyta o konkretny build, mechanikę, jak go grać lub co ulepszyć."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "archetype_name": types.Schema(
                    type=types.Type.STRING,
                    description="Nazwa archetype z indeksu, np. 'RF Chieftain', 'Bleed Slam Slayer', 'Earthshatter Berserker'",
                ),
            },
            required=["archetype_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_trade_listings",
        description=(
            "Sprawdź aktualną cenę przedmiotu lub wyszukaj oferty sprzedaży na pathofexile.com/trade. "
            "Użyj gdy gracz pyta o cenę, wartość lub chce kupić przedmiot. "
            "Wywołaj JEDEN raz dla ligi Mirage. Nie wywołuj wielokrotnie dla różnych lig."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "item_name": types.Schema(
                    type=types.Type.STRING,
                    description="Angielska nazwa przedmiotu do wyszukania",
                ),
                "league": types.Schema(
                    type=types.Type.STRING,
                    description="Pełna nazwa ligi z wielkiej litery, domyślnie 'Mirage'",
                ),
            },
            required=["item_name"],
        ),
    ),
])


async def dispatch_tool(name: str, args: dict) -> str:
    league = args.get("league", "Mirage")
    if name == "lookup_build":
        return lookup_build_data(args.get("archetype_name", ""))
    if name == "check_ninja_price":
        return await check_ninja_price(args.get("item_name", ""), league)
    if name == "search_trade_listings":
        return await search_trade_listings(args.get("item_name", ""), league)
    return f"Nieznane narzędzie: {name}"


def load_ninja_meta() -> str:
    """Ładuje topki z ninja_data jako zwięzły blok tekstu do system prompta."""
    if not _NINJA_DATA_DIR.exists():
        return ""
    lines = ["=== CURRENT META (poe.ninja, top 5 per league) ==="]
    for tag, label in [("sc", "SC"), ("hc", "HC"), ("hcssf", "HCSSF"), ("ruthless", "Ruthless")]:
        path = _NINJA_DATA_DIR / f"top_{tag}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        ts = data["scraped_at"][:10]
        lines.append(f"{label} ({data['total_chars']:,} chars, {ts}):")
        for b in data["top_builds"][:5]:
            t = "↑" if b["trend"] == 1 else ("↓" if b["trend"] == -1 else "=")
            lines.append(f"  {b['percentage']:.1f}% {b['class']} / {b['skill']} {t}")
    return "\n".join(lines)

# Baza verified buildów
_DB_PATH = Path(__file__).parent / "builds_db.yaml"
with open(_DB_PATH) as f:
    BUILDS_DB: dict = yaml.safe_load(f).get("archetypes", {})

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

_NINJA_META = load_ninja_meta()


def format_builds_index() -> str:
    """Kompaktowy indeks buildów do system prompta — tylko nazwy i tiery."""
    if not BUILDS_DB:
        return ""
    lines = ["=== BAZA BUILDÓW (użyj lookup_build by pobrać szczegóły) ==="]
    newcomers = []
    for name, data in BUILDS_DB.items():
        tier = data.get("tier", "?")
        if data.get("newcomer_friendly"):
            newcomers.append(name)
        lines.append(f"  [{tier}] {name}")
    if newcomers:
        lines.append(f"Polecane dla nowych graczy: {', '.join(newcomers)}")
    return "\n".join(lines)


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


_BUILDS_INDEX = format_builds_index()

# Krótki prompt dla pytań bez buildu (ceny, ogólne pytania)
SYSTEM_PROMPT_SHORT = f"""Jesteś ekspertem od Path of Exile na Discordzie. Odpowiadaj po polsku lub angielsku (dopasuj się do języka gracza). Konkretnie, max 200 słów. Nie zaczynaj od "Oczywiście!".
Nazwy itemów, skilii, gemów i mechanik PoE zostaw w oryginale angielskim — nie tłumacz ich na polski.
Aktualna liga SC to Mirage. Nie używaj innych nazw lig (Settlers, Necropolis itp. — to stare ligi).
Jeśli nie jesteś pewien szczegółów mechaniki, lokalizacji bossa lub konkretnego contentu — powiedz wprost że nie wiesz. Nie uzupełniaj luk kreatywnością.
Masz dostęp do narzędzi: search_trade_listings (ceny i oferty trade), lookup_build (szczegóły archetype buildu).
{_BUILDS_INDEX}"""

# Pełny prompt tylko gdy jest build context (PoB/ninja)
SYSTEM_PROMPT_FULL = f"""Jesteś ekspertem od Path of Exile pomagającym graczom na Discordzie.

## ZASADY ANALIZY BUILDU

**Weapon swap = tylko XP dla gemów.** Gemy w swapie NIE działają. Nie komentuj ich jako problemu.

**Mageblood:** 4 utility flaski permanentnie aktywne. "Gains no Charges during Effect" to normalny mod — bez znaczenia przy MB. Oceniaj flaski przez ich EFEKTY (sufiksy), nie ładunki.

**Liczby tylko z danych PoB.** Nie wymyślaj statystyk których nie ma.

**Benchmarki to wskazówki, nie wyroki.** ✅/⚠️/❌ to orientacja, nie wyroki śmierci.

**Survivability — ograniczenia PoB.** PhysicalMaximumHitTaken i ElementalMaximumHitTaken mierzą WYŁĄCZNIE ochronę przed jednym dużym hitem (one-shot protection). Większość śmierci w PoE to niefortunne kombinacje: salwy wielu hitów, degenów, curse obniżająca resy + hit (realne max hity wtedy są dramatycznie niższe niż w PoB), interakcje mechanik których PoB nie modeluje. Nie dramatyzuj z max hitami — 10k phys max hit na czerwonych mapach to akceptowalna wartość. PoE skaluje się wykładniczo: 20k phys max hit to wygodna wartość przy rozsądnym graniu (nie facetankując wszystkiego), ale wyższy budżet i nadal przebijalna — nie jest żadnym sufitem ani obowiązkowym targetem. Skale phys (orientacyjnie): <6k problematycznie, 6-12k standardowo, 12-25k solidnie, 25k+ dobrze. Skale ele: <25k słabo, 25-50k standardowo, 50-80k solidnie, 80k+ dobrze. Resy (75%/75%/75%) bezpośrednio kształtują ElementalMaximumHitTaken — to bardziej fundamentalne niż surowa liczba max hita. Bądź pokorny: PoB nie powie graczowi czy przeżyje Ulatosa z corrupted blood stackami i cursem obniżającym resy — to wymaga specyficznego config.

**Screams of the Desiccated (belt, "while affected by no Flasks"):** Build celowo NIE ma utility flaszkek z duration — przerywają shrine efekt. Ważne wyjątki: (1) Instant life/mana flaszki (mod "Instant" lub "of Instant Recovery") nie mają duration i NIE przerywają warunku — gracz może mieć 5 instant life flaszkek i nadal mieć shrine buff. (2) Tinctura to NIE flaszka — działa równolegle bez przerywania efektu, ale nie każdy build ją wykorzystuje. Jeśli gracz ma ten pasek: sama life/mana bez utility = CELOWE, nie błąd. Nie flaguj braku utility flaszkek gdy jest Screams of the Desiccated.

**Fire/Element conversion (damage taken):** Niektóre buildy konwertują otrzymywany damage jednego żywiołu na inny — np. "cold/lightning damage taken as fire" przez Sublime Vision jewel + Purity of Fire. Jeśli build ma taką mechanikę, niskie cold/lightning resist NIE są problemem — ten damage po prostu nie istnieje. Przed krytykę resistów sprawdź czy build ma taką konwersję.

**Jeśli jest sekcja "ANALIZA vs. ARCHETYPE" — dane archetype już załadowane, nie wywołuj lookup_build dla tego archetype.**

## CO ROBISZ

1. **Core mechanic** — jednym zdaniem jaka synergia napędza build
2. **Kluczowe elementy** — czy gracz ma wymagane części synergii
3. **Problemy** — każdy ❌ i ⚠️, co zmienić
4. Odpowiedz na pytanie gracza jeśli je zadał

## FORMAT

Mów po polsku lub angielsku (dopasuj się). Max 350 słów. Bullet pointy dla problemów. Nie zaczynaj od "Oczywiście!".
Nazwy itemów, skilii, gemów i mechanik PoE zostaw w oryginale angielskim — nie tłumacz ich na polski.
Jeśli nie jesteś pewien szczegółów mechaniki, lokalizacji bossa lub konkretnego contentu — powiedz wprost że nie wiesz. Nie uzupełniaj luk kreatywnością.

{_BUILDS_INDEX}

{_NINJA_META}"""


def decode_pob(code: str) -> dict | None:
    """Dekoduje PoB export code i zwraca podstawowe info o buildzie."""
    try:
        # PoB używa base64url, padding może być obcięty
        padded = code.strip() + "=="
        decoded = base64.urlsafe_b64decode(padded)
        xml_data = zlib.decompress(decoded)
        root = ET.fromstring(xml_data)

        build = root.find("Build")
        if build is None:
            return None

        # Wyciągamy podstawowe dane
        info = {
            "class": build.get("className", "Unknown"),
            "ascendancy": build.get("ascendClassName", ""),
            "level": build.get("level", "?"),
            "main_skill": build.get("mainSocketGroup", "?"),
        }

        # Statystyki z PoB
        WANTED_STATS = {
            "TotalDPS", "TotalDotDPS", "CombinedDPS",
            "Life", "EnergyShield", "Mana",
            "FireResist", "ColdResist", "LightningResist", "ChaosResist",
            "BlockChance", "SpellBlockChance", "CritChance", "AttackSpeed",
            "Armour", "Evasion", "PhysicalDamageReduction", "EnduranceCharges",
            "PhysicalMaximumHitTaken", "ElementalMaximumHitTaken", "ChaosMaximumHitTaken",
        }
        stats = {}
        for stat in build.findall("PlayerStat"):
            name = stat.get("stat")
            value = stat.get("value")
            if name in WANTED_STATS:
                stats[name] = value
        info["stats"] = stats

        # Wszystkie grupy gemów (Skills → SkillSet → Skill → Gem)
        skills = root.find("Skills")
        skill_groups = []
        if skills is not None:
            active_ss = skills.get("activeSkillSet", "1")
            active_skillsets = [ss for ss in skills.findall("SkillSet") if ss.get("id") == active_ss]
            if not active_skillsets:
                active_skillsets = skills.findall("SkillSet")[:1]
            for skillset in active_skillsets:
                for skill in skillset.findall("Skill"):
                    if skill.get("enabled") != "true":
                        continue
                    gems = [g.get("nameSpec", "") for g in skill.findall("Gem")
                            if g.get("enabled") == "true" and g.get("nameSpec")]
                    if not gems:
                        continue
                    label = re.sub(r'\^.', '', skill.get("label", "")).strip()
                    is_main = skill.get("mainActiveSkillCalcs") == "1"
                    prefix = "[MAIN] " if is_main else ""
                    skill_groups.append(f"{prefix}{label or '?'}: {', '.join(gems)}")
        info["skill_groups"] = skill_groups[:6]

        # Itemy (wyposażone) — Items → ItemSet (aktywny) → Slot
        items_section = root.find("Items")
        items = []
        if items_section is not None:
            active_set_id = items_section.get("activeItemSet", "1")
            slot_map = {}
            for item_set in items_section.findall("ItemSet"):
                if item_set.get("id") == active_set_id:
                    for slot_el in item_set.findall("Slot"):
                        item_id = slot_el.get("itemId", "")
                        slot_name = slot_el.get("name", "")
                        if item_id:
                            slot_map[item_id] = slot_name
                    break

            for item_el in items_section.findall("Item"):
                item_id = item_el.get("id", "")
                if item_id not in slot_map:
                    continue
                slot_name = slot_map[item_id]
                text = item_el.text or ""
                # Wyciągnij nazwę i kluczowe linie (pomijaj linie techniczne)
                lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
                name = ""
                mods = []
                skip_keys = {"Crafted:", "Prefix:", "Suffix:", "Quality:", "LevelReq:",
                             "Unique ID:", "Variant:", "Selected Variant:", "BasePercentile:",
                             "Influence:", "Shaper", "Elder", "League:", "Source:"}
                for line in lines:
                    if line.startswith("Rarity:"):
                        continue
                    if any(line.startswith(k) for k in skip_keys):
                        continue
                    if line.startswith("Implicits:") or line.startswith("Sockets:"):
                        continue
                    if not name:
                        name = line
                    else:
                        # Wyczyść tagi PoB ({crafted}, {range:x}, etc.)
                        clean = re.sub(r'\{[^}]+\}', '', line).strip()
                        if clean:
                            mods.append(clean)
                if name:
                    items.append(f"[{slot_name}] {name}: {' | '.join(mods[:3])}")
        info["items"] = items

        return info

    except Exception:
        return None


def format_build_context(info: dict) -> str:
    """Formatuje dane buildu do czytelnego kontekstu dla AI."""
    lines = [
        f"=== DANE BUILDU Z PATH OF BUILDING ===",
        f"Klasa: {info['class']} / {info['ascendancy']}",
        f"Poziom: {info['level']}",
    ]

    if info.get("stats"):
        lines.append("Statystyki:")
        stat_names = {
            "TotalDPS": "Hit DPS",
            "TotalDotDPS": "DoT DPS",
            "CombinedDPS": "Combined DPS",
            "Life": "Life",
            "EnergyShield": "ES",
            "Mana": "Mana",
            "FireResist": "Fire Res",
            "ColdResist": "Cold Res",
            "LightningResist": "Lightning Res",
            "ChaosResist": "Chaos Res",
            "BlockChance": "Block",
            "SpellBlockChance": "Spell Block",
            "CritChance": "Crit Chance",
            "AttackSpeed": "Atk Speed",
            "Armour": "Armour",
            "Evasion": "Evasion",
            "PhysicalDamageReduction": "Phys Reduction",
            "EnduranceCharges": "Endurance Charges",
            "PhysicalMaximumHitTaken": "Phys Max Hit",
            "ElementalMaximumHitTaken": "Elemental Max Hit",
            "ChaosMaximumHitTaken": "Chaos Max Hit",
        }
        for key, label in stat_names.items():
            if key in info["stats"]:
                lines.append(f"  {label}: {info['stats'][key]}")

    if info.get("skill_groups"):
        lines.append("Skille:")
        for sg in info["skill_groups"]:
            lines.append(f"  {sg}")

    if info.get("items"):
        lines.append("Wyposażenie:")
        for item in info["items"]:
            lines.append(f"  {item}")

    lines.append("=" * 38)
    return "\n".join(lines)


def detect_archetype(info: dict) -> tuple[str, dict] | tuple[None, None]:
    """Dopasowuje build do archetype z bazy. Zwraca (name, archetype_data) lub (None, None).
    Scoring: wygrywa archetype z największą liczbą dopasowanych skill_keywords.
    Puste skill_keywords = fallback (score 0), zawsze przegrywa z bardziej konkretnym matchem."""
    all_gems = " ".join(info.get("skill_groups", []))
    ascendancy = info.get("ascendancy", "")

    best_name, best_data, best_score = None, None, -1

    for name, data in BUILDS_DB.items():
        det = data.get("detection", {})
        skill_kw = det.get("skill_keywords", [])
        asc_kw = det.get("ascendancy_keywords", [])

        asc_match = not asc_kw or any(kw.lower() in ascendancy.lower() for kw in asc_kw)
        if not asc_match:
            continue

        skill_hits = sum(1 for kw in skill_kw if kw.lower() in all_gems.lower())
        # Jeśli są skill_keywords ale żaden nie pasuje — odrzuć
        if skill_kw and skill_hits == 0:
            continue

        if skill_hits > best_score:
            best_score, best_name, best_data = skill_hits, name, data

    return (best_name, best_data) if best_name else (None, None)


def format_archetype_context(archetype_name: str, data: dict, stats: dict) -> str:
    """Formatuje porównanie buildu z benchmarkami archetype."""
    newcomer = data.get("newcomer_friendly", False)
    label = f"{archetype_name} [POLECANY DLA NOWYCH GRACZY]" if newcomer else archetype_name
    lines = [f"=== ANALIZA vs. ARCHETYPE: {label} ==="]

    benchmarks = data.get("benchmarks", {})
    issues = []
    goods = []

    for stat_key, bench in benchmarks.items():
        user_val_str = stats.get(stat_key)
        if user_val_str is None:
            continue
        try:
            user_val = float(user_val_str)
        except ValueError:
            continue

        mn = bench.get("min")
        good = bench.get("good")
        unit = bench.get("unit", "")
        note = bench.get("note", "")

        if mn and user_val < mn:
            issues.append(f"  ❌ {stat_key}: {user_val:.0f}{unit} (minimum: {mn}{unit}, cel: {good}{unit}){' — ' + note if note else ''}")
        elif good and user_val < good:
            issues.append(f"  ⚠️  {stat_key}: {user_val:.0f}{unit} (ok, ale cel to {good}{unit}){' — ' + note if note else ''}")
        else:
            goods.append(f"  ✅ {stat_key}: {user_val:.0f}{unit}")

    if issues:
        lines.append("Problemy:")
        lines.extend(issues)
    if goods:
        lines.append("Ok:")
        lines.extend(goods)

    notes = data.get("notes", [])
    if notes:
        lines.append("Wskazówki:")
        for n in notes[:3]:
            lines.append(f"  • {n}")

    lines.append("=" * 40)
    return "\n".join(lines)


async def fetch_ninja_character(account: str, charname: str, league_url: str = "mirage") -> dict | None:
    """Pobiera dane postaci z poe.ninja character API."""
    async with httpx.AsyncClient(headers=_NINJA_HEADERS, timeout=15) as client:
        # Pobierz wersję snapshotu dla ligi
        r = await client.get("https://poe.ninja/poe1/api/data/index-state")
        if r.status_code != 200:
            return None
        version = None
        for snap in r.json().get("snapshotVersions", []):
            if snap["url"] == league_url and snap["type"] == "exp":
                version = snap["version"]
                break
        if not version:
            return None

        r = await client.get(
            f"https://poe.ninja/poe1/api/builds/{version}/character",
            params={"account": account, "name": charname, "overview": league_url, "type": "exp"},
        )
        if r.status_code != 200:
            return None
        return r.json()


def format_ninja_context(data: dict, account: str, charname: str) -> str:
    """Formatuje odpowiedź ninja character API jako kontekst buildu."""
    lines = [
        f"=== DANE POSTACI Z POE.NINJA ===",
        f"Account: {account} / {charname}",
    ]

    char = data.get("character", {}) or {}
    if char:
        lines.append(f"Klasa: {char.get('class', '?')} / {char.get('ascendancy', '?')}")
        lines.append(f"Poziom: {char.get('level', '?')}")

    # PoB stats (liczone przez ninja)
    pob = data.get("data", {}) or {}
    if pob:
        lines.append("Statystyki (PoB):")
        for key in ["Life", "EnergyShield", "TotalDPS", "CombinedDPS", "TotalDotDPS",
                    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
                    "Armour", "Evasion", "BlockChance"]:
            if key in pob:
                lines.append(f"  {key}: {pob[key]}")

    # Items
    items = data.get("items", []) or []
    if items:
        lines.append("Wyposażenie:")
        for item in items[:12]:
            slot = item.get("inventoryId", item.get("slot", "?"))
            name = item.get("name", "") or item.get("typeLine", "?")
            mods = (item.get("explicitMods") or [])[:3]
            suffix = ": " + " | ".join(mods) if mods else ""
            lines.append(f"  [{slot}] {name}{suffix}")

    # Skills
    skills = data.get("skills", []) or []
    if skills:
        lines.append("Skille:")
        for skill in skills[:6]:
            gems = [g.get("skill") or g.get("name", "") for g in (skill.get("gems") or [])]
            label = skill.get("label", "?")
            lines.append(f"  {label}: {', '.join(g for g in gems if g)}")

    lines.append("=" * 35)
    return "\n".join(lines)


def extract_ninja_ref(message: str) -> tuple[str, str, str] | tuple[None, None, None]:
    """Wyciąga (account, charname, league_url) z wiadomości.
    Obsługuje: !ninja ACCOUNT/CHAR [liga], poe.ninja/character/ACCOUNT/CHAR"""
    # !ninja ACCOUNT/CHAR [liga] — bot stripa ! przed przekazaniem, więc szukamy bez !
    m = re.search(r'(?:^|!)ninja\s+(\S+)/(\S+)(?:\s+(\S+))?', message)
    if m:
        account, char, league_hint = m.group(1), m.group(2), m.group(3)
        league_url = NINJA_LEAGUES.get((league_hint or "sc").lower(), "mirage")
        return account, char, league_url

    # poe.ninja/poe1/profile/ACCOUNT/character/CHAR
    m = re.search(r'poe\.ninja\S*/profile/([^/\s?]+)/character/([^/\s?]+)', message)
    if m:
        return m.group(1), m.group(2), "mirage"

    # poe.ninja/*/*/character/ACCOUNT/CHAR (dowolna ścieżka przed /character/)
    m = re.search(r'poe\.ninja\S*/character/([^/\s?]+)/([^/\s?]+)', message)
    if m:
        league_m = re.search(r'/builds/([^/\s?]+)/character/', message)
        league_hint = league_m.group(1) if league_m else "sc"
        league_url = NINJA_LEAGUES.get(league_hint.lower(), "mirage")
        return m.group(1), m.group(2), league_url

    return None, None, None


async def fetch_pobb_in_code(url: str) -> str | None:
    """Pobiera surowy PoB code z pobb.in URL. Używa cache."""
    if url in _pobb_cache:
        print(f"[pobb.in] cache hit: {url}")
        return _pobb_cache[url]

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            # poe.ninja /pob/
            if "poe.ninja" in url and "/pob/" in url:
                slug = url.rstrip("/").split("/")[-1]
                game = "poe1" if "/poe1/" in url else "poe2"
                r = await client.get(f"https://poe.ninja/{game}/pob/raw/{slug}")
                if r.status_code == 200 and len(r.text.strip()) > 100:
                    code = r.text.strip()
                    _pobb_cache[url] = code
                    return code
                return None

            # maxroll.gg
            if "maxroll.gg" in url:
                slug = url.rstrip("/").split("/")[-1]
                r = await client.get(f"https://maxroll.gg/poe/api/pob/{slug}")
                if r.status_code == 200 and len(r.text.strip()) > 100:
                    code = r.text.strip()
                    _pobb_cache[url] = code
                    return code
                return None

            # Próbuj /raw endpoint najpierw
            slug = url.rstrip("/").split("/")[-1]
            raw_url = f"https://pobb.in/{slug}/raw"
            r = await client.get(raw_url)
            if r.status_code == 200 and len(r.text.strip()) > 100:
                code = r.text.strip()
                _pobb_cache[url] = code
                return code

            # Fallback: wyciągnij base64 kod z HTML strony
            r = await client.get(url)
            if r.status_code == 200:
                match = re.search(r'[A-Za-z0-9+/\-_]{100,}={0,2}', r.text)
                if match:
                    code = match.group(0)
                    _pobb_cache[url] = code
                    return code
    except Exception as e:
        print(f"[pobb.in] błąd fetchowania: {e}")
    return None


def extract_pob_code(message: str) -> tuple[str | None, str | None]:
    """Wyciąga PoB code lub URL z wiadomości.
    Zwraca (raw_code, pobb_url) — jedno z nich będzie None."""
    # Wykryj pobb.in, maxroll.gg lub poe.ninja/pob URL
    pobb_match = re.search(r'https?://(?:pobb\.in|maxroll\.gg/poe/pob|poe\.ninja/poe\d/pob)/\S+', message)
    if pobb_match:
        return None, pobb_match.group(0)

    # Surowy PoB code — długi base64 string
    words = message.split()
    for word in words:
        clean = word.strip(".,;:!?\"'")
        if len(clean) > 100 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_" for c in clean):
            return clean, None

    return None, None


@bot.event
async def on_ready():
    print(f"UberPoB online jako {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    print(f"[DEBUG] wiadomość od {message.author}: {message.content[:50]}")
    if message.author.bot:
        return

    # Reaguj na wzmianki, ! prefix lub wklejony link poe.ninja/pobb.in
    has_ninja_link = "poe.ninja" in message.content and "/character/" in message.content
    has_pobb_link = "pobb.in/" in message.content
    if not (bot.user in message.mentions or message.content.startswith("!") or has_ninja_link or has_pobb_link):
        return

    # Wyczyść wzmiankę i prefix ! z treści
    content = message.content.replace(f"<@{bot.user.id}>", "").lstrip("!").strip()
    # Discord owijania URL-ów w <> (suppress embed) — odkryj je
    content = re.sub(r'<(https?://[^>]+)>', r'\1', content)
    if not content:
        await message.reply("Wklej kod buildu z PoB albo zadaj pytanie o Path of Exile!")
        return

    async with message.channel.typing():
        # Sprawdź czy jest ninja character ref
        ninja_account, ninja_char, ninja_league = extract_ninja_ref(content)
        build_context = ""

        if ninja_account:
            await message.reply(f"🔍 Szukam {ninja_account}/{ninja_char} na poe.ninja...")
            ninja_data = await fetch_ninja_character(ninja_account, ninja_char, ninja_league)
            if ninja_data:
                pob_code = ninja_data.get("pathOfBuildingExport")
                info = decode_pob(pob_code) if pob_code else None
                if info:
                    build_context = format_build_context(info)
                    arch_name, arch_data = detect_archetype(info)
                    if arch_name:
                        build_context += "\n\n" + format_archetype_context(arch_name, arch_data, info.get("stats", {}))
                    header = (f"Postać: {ninja_account}/{ninja_char} | "
                              f"{ninja_data.get('class', '')} {ninja_data.get('ascendancyClassName', '')} "
                              f"lvl {ninja_data.get('level', '?')} | poe.ninja\n\n")
                    build_context = header + build_context
                else:
                    build_context = format_ninja_context(ninja_data, ninja_account, ninja_char)
                content = re.sub(r'!ninja\s+\S+/\S+(?:\s+\S+)?', '', content).strip()
                content = re.sub(r'https?://\S*poe\.ninja\S*', '', content).strip()
            else:
                await message.reply(f"Nie znalazłem {ninja_account}/{ninja_char} na poe.ninja. Sprawdź czy postać jest widoczna w tej lidze.")
                return

        # Sprawdź czy jest kod PoB lub link (tylko jeśli nie ma kontekstu z ninja)
        if not build_context:
            raw_code, pobb_url = extract_pob_code(content)

            if pobb_url:
                await message.reply("📥 Pobieram build z pobb.in...")
                print(f"[STAGE] fetch pobb.in start")
                raw_code = await fetch_pobb_in_code(pobb_url)
                print(f"[STAGE] fetch pobb.in done, got {len(raw_code) if raw_code else 0} chars")
                if not raw_code:
                    await message.reply("Nie mogłem pobrać buildu z pobb.in — sprawdź link.")
                    return
                content = content.replace(pobb_url, "[PoB link]").strip()

            if raw_code:
                print(f"[STAGE] decode_pob start")
                info = decode_pob(raw_code)
                print(f"[STAGE] decode_pob done: {bool(info)}")
                if info:
                    build_context = format_build_context(info)
                    arch_name, arch_data = detect_archetype(info)
                    if arch_name:
                        print(f"[STAGE] archetype: {arch_name}")
                        arch_context = format_archetype_context(arch_name, arch_data, info.get("stats", {}))
                        build_context = build_context + "\n\n" + arch_context
                    content = content.replace(raw_code, "[PoB code]").strip()
                else:
                    await message.reply("Nie mogłem odczytać kodu PoB — sprawdź czy skopiowałeś całość.")
                    return

        user_message = content
        if build_context:
            user_message = f"{build_context}\n\nPytanie: {content}" if content != "[PoB code]" else f"{build_context}\n\nZrób szczegółową analizę tego buildu. Oceń: 1) survivability — używaj PhysicalMaximumHitTaken, ElementalMaximumHitTaken i poziom ele resów jako główne metryki, NIE surowego Armouru ani Evasion, 2) DPS i główny skill, 3) co konkretnie wymaga poprawy i dlaczego, 4) priorytety — co naprawić najpierw."
        print(f"[STAGE] gemini call start, context {len(user_message)} chars")

        try:
            contents = [types.Content(role="user", parts=[types.Part(text=user_message)])]
            reply = None

            for _round in range(6):  # max 5 tool rounds + 1 final
                response = await gemini.aio.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT_FULL if build_context else SYSTEM_PROMPT_SHORT,
                        temperature=1.0,
                        tools=[_POE_TOOLS],
                    ),
                )
                if not response.candidates:
                    break
                candidate = response.candidates[0]
                parts = candidate.content.parts if candidate.content else []
                fn_calls = [p for p in parts if hasattr(p, "function_call") and p.function_call]
                if not fn_calls:
                    reply = response.text
                    um = response.usage_metadata
                    if um:
                        print(f"[TOKENS] in={um.prompt_token_count} out={um.candidates_token_count} total={um.total_token_count}")
                    break
                fn_responses = []
                for p in fn_calls:
                    fc = p.function_call
                    print(f"[TOOL] {fc.name}({dict(fc.args)})")
                    result = await dispatch_tool(fc.name, dict(fc.args))
                    fn_responses.append(types.Part(
                        function_response=types.FunctionResponse(name=fc.name, response={"result": result})
                    ))
                contents.append(types.Content(role="model", parts=parts))
                contents.append(types.Content(role="user", parts=fn_responses))
            else:
                reply = None

            if not reply:
                finish = response.candidates[0].finish_reason if response.candidates else "unknown"
                print(f"[WARN] pusta odpowiedź, finish_reason: {finish}")
                await message.reply(f"Gemini zwrócił pustą odpowiedź (finish_reason: {finish}). Spróbuj jeszcze raz.")
                return

            # Discord ma limit 2000 znaków — tnij na kawałki po newline
            chunks = []
            current = ""
            for line in reply.splitlines(keepends=True):
                if len(current) + len(line) > 1900:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current += line
            if current:
                chunks.append(current)

            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)

        except Exception as e:
            await message.reply(f"Błąd API: {e}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Brak DISCORD_TOKEN w .env!")
    else:
        bot.run(token)
