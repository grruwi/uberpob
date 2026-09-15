"""Reddit feed — najlepszy post co godzinę + komenda !reddit do ręcznego browsowania."""

import os
import json
from pathlib import Path

import httpx
import discord
from discord.ext import tasks

SUBS = [
    "PathOfExile",
    "PathOfExile2",
    "ProgrammerHumor",
    "shitposting",
]

# tylko te suby lądują na kanale automatycznie, reszta tylko przez !reddit
AUTO_SUBS = {"PathOfExile", "PathOfExile2"}

HEADERS = {"User-Agent": "UberPoB-RedditFeed/1.0 (by grruwi)"}
SEEN_FILE = Path(__file__).parent / ".reddit_seen.json"
MIN_SCORE = 50


def _load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def _save_seen(seen: set[str]) -> None:
    trimmed = sorted(seen)[-500:]
    SEEN_FILE.write_text(json.dumps(trimmed))


async def _fetch_sub(sub: str, limit: int = 10) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()["data"]["children"]


def _is_image(post: dict) -> bool:
    return post.get("post_hint") == "image" or post["url"].endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp")
    )


def _make_embed(post: dict, sub: str) -> discord.Embed:
    title = post["title"][:256]
    permalink = f"https://reddit.com{post['permalink']}"
    score = post["score"]
    comments = post["num_comments"]
    flair = post.get("link_flair_text", "")

    embed = discord.Embed(
        title=title,
        url=permalink,
        color=discord.Color.orange(),
    )
    embed.set_author(name=f"r/{sub}", url=f"https://reddit.com/r/{sub}")
    embed.set_footer(text=f"⬆ {score}  |  💬 {comments}" + (f"  |  {flair}" if flair else ""))

    if _is_image(post):
        embed.set_image(url=post["url"])

    return embed


class RedditFeed:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.seen = _load_seen()
        self.channel_id: int | None = None

    def start(self) -> None:
        channel_id_str = os.getenv("REDDIT_CHANNEL_ID")
        if not channel_id_str:
            print("⚠ REDDIT_CHANNEL_ID nie ustawiony w .env — Reddit feed wyłączony")
            return
        self.channel_id = int(channel_id_str)
        print(f"✅ Reddit feed gotowy → kanał {self.channel_id} (tylko !reddit, auto-post wyłączony)")

    def stop(self) -> None:
        if self._feed_loop.is_running():
            self._feed_loop.cancel()

    async def _get_best_unseen(self) -> tuple[str, dict] | None:
        """Zbierz posty z PoE subów, zwróć najlepszy niewidziany."""
        candidates: list[tuple[str, dict]] = []

        for sub in AUTO_SUBS:
            try:
                posts = await _fetch_sub(sub)
            except Exception as e:
                print(f"Reddit feed: błąd r/{sub}: {e}")
                continue

            for post_wrapper in posts:
                post = post_wrapper["data"]
                if post["id"] in self.seen:
                    continue
                if post["stickied"]:
                    continue
                if post["score"] < MIN_SCORE:
                    continue
                candidates.append((sub, post))

        if not candidates:
            return None

        # najwyższy score wygrywa
        candidates.sort(key=lambda x: x[1]["score"], reverse=True)
        return candidates[0]

    @tasks.loop(hours=4)
    async def _feed_loop(self) -> None:
        """Co godzinę — jeden najlepszy post."""
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            print(f"❌ Reddit feed: kanał {self.channel_id} nie znaleziony")
            return

        best = await self._get_best_unseen()
        if not best:
            return

        sub, post = best
        embed = _make_embed(post, sub)
        await channel.send(embed=embed)

        self.seen.add(post["id"])
        _save_seen(self.seen)
        print(f"Reddit feed: r/{sub} — {post['title'][:60]}")

    @_feed_loop.before_loop
    async def _before_feed(self) -> None:
        await self.bot.wait_until_ready()

    async def manual_feed(self, channel: discord.TextChannel, count: int = 5) -> None:
        """!reddit — ręczny dump najlepszych postów."""
        all_posts: list[tuple[str, dict]] = []

        for sub in SUBS:
            try:
                posts = await _fetch_sub(sub)
            except Exception as e:
                await channel.send(f"❌ r/{sub}: {e}")
                continue

            for post_wrapper in posts:
                post = post_wrapper["data"]
                if post["stickied"]:
                    continue
                if post["score"] < MIN_SCORE:
                    continue
                all_posts.append((sub, post))

        if not all_posts:
            await channel.send("Pusto — nic ciekawego na Reddicie (impossible, ale ok)")
            return

        all_posts.sort(key=lambda x: x[1]["score"], reverse=True)
        for sub, post in all_posts[:count]:
            embed = _make_embed(post, sub)
            await channel.send(embed=embed)
            self.seen.add(post["id"])

        _save_seen(self.seen)
