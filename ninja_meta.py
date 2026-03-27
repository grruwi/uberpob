#!/usr/bin/env python3
"""
ninja_meta.py — Scraper topek buildów z poe.ninja

Endpoints (odkryte z JS bundle):
  /poe1/api/data/index-state       → wersje snapshotów per liga (wersja + URL slug)
  /poe1/api/data/build-index-state → top builds per liga (class+skill+%, total chars)
  /poe1/api/builds/{version}/character?account=X&name=Y&overview=Z&type=W → konkretna postać

Uruchamianie:
  uv run python ninja_meta.py
  uv run python ninja_meta.py --character ACCOUNT/CHARNAME [--league mirage]
"""

import httpx
import json
import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://poe.ninja"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://poe.ninja/poe1/builds",
    "Accept": "application/json",
}

TARGET_LEAGUES = {
    "mirage":     "sc",
    "miragehc":   "hc",
    "miragehcssf": "hcssf",
    "mirager":    "ruthless",
}

OUTPUT_DIR = Path(__file__).parent / "ninja_data"


async def fetch_build_stats(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"{BASE_URL}/poe1/api/data/build-index-state")
    resp.raise_for_status()
    data = resp.json()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUTPUT_DIR.mkdir(exist_ok=True)

    for league_data in data["leagueBuilds"]:
        url = league_data["leagueUrl"]
        if url not in TARGET_LEAGUES:
            continue

        tag = TARGET_LEAGUES[url]
        output = {
            "scraped_at": ts,
            "league": league_data["leagueName"],
            "league_url": url,
            "total_chars": league_data["total"],
            # statistics: list of {class, skill, percentage, trend}
            # trend: 1=rosnący, -1=malejący, 0=stabilny
            "top_builds": league_data["statistics"],
        }

        fname = OUTPUT_DIR / f"top_{tag}.json"
        fname.write_text(json.dumps(output, indent=2, ensure_ascii=False))
        print(f"[{tag.upper():6s}] {fname.name}  —  {league_data['total']:,} postaci, {len(league_data['statistics'])} buildów")


async def fetch_character(client: httpx.AsyncClient, account: str, charname: str, league_url: str = "mirage") -> dict:
    """
    Pobiera dane konkretnej postaci z poe.ninja.
    Wymaga najpierw pobrania version z index-state dla danej ligi.
    """
    # Pobierz aktualną wersję snapshoту dla ligi
    resp = await client.get(f"{BASE_URL}/poe1/api/data/index-state")
    resp.raise_for_status()
    index = resp.json()

    version = None
    for snap in index.get("snapshotVersions", []):
        if snap["url"] == league_url and snap["type"] == "exp":
            version = snap["version"]
            break

    if not version:
        raise ValueError(f"Nie znaleziono snapshoту dla ligi: {league_url}")

    # Pobierz postać
    resp = await client.get(
        f"{BASE_URL}/poe1/api/builds/{version}/character",
        params={
            "account": account,
            "name": charname,
            "overview": league_url,
            "type": "exp",
        },
    )
    resp.raise_for_status()
    return resp.json()


async def main(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        if args.character:
            # Tryb: pobierz konkretną postać
            parts = args.character.split("/", 1)
            if len(parts) != 2:
                print("Użycie: --character ACCOUNT/CHARNAME")
                return
            account, charname = parts
            league = args.league or "mirage"
            print(f"Pobieranie postaci {account}/{charname} z ligi {league}...")
            data = await fetch_character(client, account, charname, league)
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            # Tryb domyślny: scrape topek per liga
            print("Scrapowanie topek buildów z poe.ninja...")
            await fetch_build_stats(client)
            print("Gotowe.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="poe.ninja meta scraper")
    parser.add_argument("--character", help="ACCOUNT/CHARNAME — pobierz konkretną postać")
    parser.add_argument("--league", help="Liga URL slug (default: mirage)", default="mirage")
    asyncio.run(main(parser.parse_args()))
