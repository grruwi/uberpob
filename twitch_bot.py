"""UberPoB — moduł Twitch. Podpina tego samego bota co Discord do Twitch IRC."""

import os
import re
import time
import random
import asyncio
from collections import defaultdict

import twitchio
from twitchio.ext import commands


class RateLimiter:
    """Per-user rate limiting: cooldown + max requests/hour."""

    def __init__(self, cooldown: int = 30, max_per_hour: int = 15):
        self.cooldown = cooldown
        self.max_per_hour = max_per_hour
        self._last_use: dict[str, float] = {}
        self._hourly_counts: dict[str, list[float]] = defaultdict(list)

    def check(self, user: str) -> str | None:
        """Returns None if OK, or a reason string if rate-limited."""
        now = time.time()
        user = user.lower()

        # Cooldown
        last = self._last_use.get(user, 0)
        if now - last < self.cooldown:
            wait = int(self.cooldown - (now - last))
            return f"@{user} poczekaj {wait}s zanim zapytasz ponownie."

        # Hourly cap — prune old entries
        hour_ago = now - 3600
        self._hourly_counts[user] = [t for t in self._hourly_counts[user] if t > hour_ago]
        if len(self._hourly_counts[user]) >= self.max_per_hour:
            return f"@{user} osiągnąłeś limit {self.max_per_hour} zapytań/h. Spróbuj za chwilę."

        # OK — record usage
        self._last_use[user] = now
        self._hourly_counts[user].append(now)
        return None


class TwitchBot(commands.Bot):
    """Twitch frontend for UberPoB. Delegates to the same handler as Discord."""

    # StreamElements & other Twitch bot commands — ignore so UberPoB doesn't
    # try to answer "!followage" as a PoE question
    _IGNORED_COMMANDS: set[str] = {
        # SE custom
        "ign", "profile", "starter", "discord", "cipa", "suchar",
        "siema", "elko", "elo", "cześć", "czesc", "hi",
        "ile", "kuciapa", "knaga", "delve", "delviarz",
        # SE default (enabled)
        "docs", "followage", "ping", "queue", "raffle_cancel", "raffle_join",
        "setgame", "songrequest", "timer", "vanish", "voteskip", "watchtime",
        # SE default (common, often re-enabled)
        "points", "top", "uptime", "commands", "8ball", "roulette", "slots",
        "quote", "so", "lurk",
    }

    def __init__(self, handle_message_fn, token: str, channel: str):
        super().__init__(
            token=token,
            prefix="!",
            initial_channels=[channel],
        )
        self._handle = handle_message_fn
        self._channel = channel.lower()
        self._limiter = RateLimiter(cooldown=30, max_per_hour=15)

    async def event_ready(self):
        print(f"[Twitch] Połączony jako {self.nick} na #{self._channel}")
        asyncio.create_task(self._random_messages_loop())

    async def _random_messages_loop(self):
        """Co 15-25 minut losowa postać pisze na chacie."""
        # Import here to avoid circular imports
        from google import genai
        from google.genai import types
        from dotenv import load_dotenv
        load_dotenv()

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        client = genai.Client(vertexai=True, project=project_id, location=location)

        characters = [
            ("Izaro", "Jesteś Izaro, tyran Labiryntu. Mów dostojnie, dramatycznie, z pogardą dla słabości. Max 300 znaków."),
            ("Einhar", "Jesteś Einhar, łowca bestii. Entuzjastyczny, głośny, wszystko to polowanie. 'Stupid beast!' Max 300 znaków."),
            ("Niko", "Jesteś Niko, górnik obsesyjnie szukający sulphite. Wszystko porównujesz do wydobycia i ciemności. Max 300 znaków."),
            ("Alva", "Jesteś Alva Valai, arqueolożka inkursji. Pewna siebie do granic arogancji, fascynuje cię historia Vaal. Max 300 znaków."),
            ("Zana", "Jesteś Zana, kartografka Atlasu. Filozofujesz o naturze map, melancholijna, mądra. Max 300 znaków."),
            ("Jenebu", "Jesteś Jenebu, zbannowany król TFT. Wściekły, niestabilny, obsesyjny. Twoje imperium runęło. Max 300 znaków."),
        ]

        prompts = [
            "Skomentuj fakt że streamer gra w PoE zamiast spać.",
            "Powiedz coś o aktualnym stanie ekonomii w PoE.",
            "Daj radę życiową w swoim stylu.",
            "Skomentuj że chat jest cicho.",
            "Zadaj chatu pytanie.",
            "Opowiedz krótką anegdotę ze swojego życia w Wraeclast.",
            "Wyrażaj opinię o tym że ludzie oglądają kogoś grającego w grę zamiast grać sami.",
            "Narzekaj na coś.",
            "Powiedz coś niespodziewanego i kompletnie nie na temat.",
            "Pochwal streamera, ale tak żeby to brzmiało jak obelga.",
        ]

        await asyncio.sleep(60)  # Wait 1 min after start before first message
        while True:
            delay = random.randint(15 * 60, 25 * 60)  # 15-25 min
            await asyncio.sleep(delay)
            try:
                name, persona = random.choice(characters)
                prompt = random.choice(prompts)
                response = await asyncio.get_event_loop().run_in_executor(None, lambda: client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                    config=types.GenerateContentConfig(
                        system_instruction=persona + " Odpowiadaj po polsku. Nie używaj cudzysłowów. Jedna wiadomość.",
                        temperature=1.6,
                    ),
                ))
                msg = (response.text or "").strip().strip('"')[:450]
                if msg:
                    channel = self.get_channel(self._channel)
                    if channel:
                        await channel.send(f"[{name}] {msg}")
                        print(f"[Twitch] Random message from {name}: {msg[:80]}...", flush=True)
            except Exception as e:
                print(f"[Twitch] Random message error: {e}", flush=True)

    # Boty których wiadomości ignorujemy całkowicie
    _IGNORED_USERS: set[str] = {
        "streamelements", "nightbot", "moobot", "fossabot", "streamlabs",
        "soundalerts", "pokemoncommunitygame", "buttsbot", "wizebot",
    }

    async def event_message(self, message: twitchio.Message):
        # Ignoruj własne wiadomości
        if message.echo:
            return

        # Ignoruj wiadomości od innych botów
        author_name = (message.author.name or "").lower() if message.author else ""
        if author_name in self._IGNORED_USERS:
            return

        content = message.content or ""

        # Reaguj na: !komendy (ale nie SE/inne boty), linki PoB/ninja, @mention
        has_command = False
        if content.startswith("!"):
            cmd_name = content[1:].split()[0].lower() if len(content) > 1 else ""
            has_command = cmd_name not in self._IGNORED_COMMANDS
        has_pob_link = bool(re.search(
            r'(?:pobb\.in/|maxroll\.gg/poe2?/pob/|poe\.ninja/poe\d/pob/)', content
        ))
        has_ninja_link = "poe.ninja" in content and "/character/" in content
        # bot_mentioned wyłączony — bot jest zalogowany jako streamer (grruwi),
        # więc każdy @grruwi na czacie fałszywie triggeruje bota
        bot_mentioned = False

        if not (has_command or has_pob_link or has_ninja_link or bot_mentioned):
            return

        # Rate limit — broadcaster is exempt
        author = message.author.name if message.author else "unknown"
        is_broadcaster = author.lower() == self._channel
        limit_msg = None if is_broadcaster else self._limiter.check(author)
        if limit_msg:
            await message.channel.send(limit_msg)
            return

        # Strip prefix/mention
        text = content
        if has_command:
            text = content[1:]  # Remove !
        if bot_mentioned and self.nick:
            text = re.sub(re.escape(self.nick), "", text, flags=re.IGNORECASE).strip()

        # Check if this is a build query (PoB link, ninja link, or build-related)
        has_build = bool(re.search(
            r'pobb\.in/|maxroll\.gg/poe|poe\.ninja/poe\d/pob/|poe\.ninja.*character/|ninja\s+\S+/\S+',
            text, re.IGNORECASE
        ))

        # Delegate to shared handler
        async def reply_fn(msg: str):
            max_msgs = 3 if has_build else 1
            max_chars = 490
            # Split on word boundaries
            chunks = []
            remaining = msg
            while remaining and len(chunks) < max_msgs:
                if len(remaining) <= max_chars:
                    chunks.append(remaining)
                    break
                # Find last space before limit
                cut = remaining.rfind(' ', 0, max_chars)
                if cut <= 0:
                    cut = max_chars
                chunks.append(remaining[:cut])
                remaining = remaining[cut:].lstrip()
            for chunk in chunks:
                await message.channel.send(chunk)
                if len(chunks) > 1:
                    await asyncio.sleep(1.5)

        try:
            # Immediate feedback so user knows bot is working
            await message.channel.send(f"@{author} 🤔")
            await self._handle(text, reply_fn, source="twitch")
        except Exception as e:
            print(f"[Twitch] Error handling message from {author}: {e}")
            await message.channel.send(f"@{author} coś poszło nie tak, spróbuj ponownie.")
