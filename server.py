"""MCP server for the Microsoft Fabric community update blogs (Power BI + Fabric).

Reads the public RSS feeds of the two boards, caches them in memory and exposes
tools for listing, searching and reading posts over streamable HTTP.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

RSS_URL = "https://community.fabric.microsoft.com/t5/s/rss/board?board.id={board}"
BLOGS = {
    "powerbi": {
        "board": "fbc_pbiupdatesblog",
        "name": "Power BI Updates Blog",
        "url": "https://community.fabric.microsoft.com/category/fabricupdatesblogs/blog/fbc_pbiupdatesblog",
    },
    "fabric": {
        "board": "fbc_fabricupdatesblogs",
        "name": "Fabric Updates Blog",
        "url": "https://community.fabric.microsoft.com/category/fabricupdatesblogs/blog/fbc_fabricupdatesblogs",
    },
}
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "900"))
USER_AGENT = "fabric-pbi-blog-mcp/1.0 (+https://github.com/bredeespelid/fabric-pbi-blog-mcp)"
DC = "{http://purl.org/dc/elements/1.1/}"


@dataclass
class Post:
    id: str
    blog: str
    title: str
    url: str
    author: str
    published: datetime
    text: str

    def summary(self, chars: int = 300) -> dict:
        snippet = self.text[:chars].rstrip() + ("…" if len(self.text) > chars else "")
        return {
            "id": self.id,
            "blog": self.blog,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "published": self.published.isoformat(),
            "summary": snippet,
        }


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in {"h1", "h2", "h3", "h4"}:
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self._href = href

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and getattr(self, "_href", None):
            self.parts.append(f" ({self._href})")
            self._href = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_feed(blog: str, xml: bytes) -> list[Post]:
    posts = []
    for item in ET.fromstring(xml).iter("item"):
        url = (item.findtext("guid") or item.findtext("link") or "").strip()
        match = re.search(r"/ba-p/(\d+)", url)
        pub = item.findtext("pubDate")
        posts.append(
            Post(
                id=match.group(1) if match else url,
                blog=blog,
                title=(item.findtext("title") or "").strip(),
                url=url,
                author=(item.findtext(f"{DC}creator") or "").strip(),
                published=parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc),
                text=html_to_text(item.findtext("description") or ""),
            )
        )
    return posts


class FeedCache:
    """Keeps every post seen since start, so history grows beyond the 20-item feed."""

    def __init__(self) -> None:
        self.posts: dict[str, Post] = {}
        self.fetched_at: dict[str, float] = {}
        self.lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> None:
        async with self.lock:
            stale = [b for b in BLOGS if force or time.time() - self.fetched_at.get(b, 0) > CACHE_TTL]
            if not stale:
                return
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
                results = await asyncio.gather(
                    *(client.get(RSS_URL.format(board=BLOGS[b]["board"])) for b in stale),
                    return_exceptions=True,
                )
            errors = []
            for blog, resp in zip(stale, results):
                try:
                    if isinstance(resp, Exception):
                        raise resp
                    resp.raise_for_status()
                    for post in parse_feed(blog, resp.content):
                        self.posts[post.id] = post
                    self.fetched_at[blog] = time.time()
                except Exception as exc:  # keep serving cached data if a feed is down
                    errors.append(f"{blog}: {exc}")
            if errors and not self.posts:
                raise RuntimeError("Could not fetch blog feeds: " + "; ".join(errors))

    async def select(self, blog: str) -> list[Post]:
        await self.refresh()
        if blog not in ("all", *BLOGS):
            raise ValueError(f"Unknown blog '{blog}'. Use one of: all, {', '.join(BLOGS)}")
        posts = [p for p in self.posts.values() if blog == "all" or p.blog == blog]
        return sorted(posts, key=lambda p: p.published, reverse=True)


cache = FeedCache()
mcp = FastMCP(
    "fabric-pbi-blog",
    instructions=(
        "News from the official Microsoft Power BI Updates Blog and Fabric Updates Blog "
        "(community.fabric.microsoft.com). Use latest_posts for what's new, search_posts to find "
        "posts on a topic, and get_post to read a full article. blog is 'powerbi', 'fabric' or 'all'."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
)


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    dt = datetime.fromisoformat(since)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@mcp.tool()
async def list_blogs() -> list[dict]:
    """List the blogs this server covers."""
    return [{"blog": key, "name": b["name"], "url": b["url"]} for key, b in BLOGS.items()]


@mcp.tool()
async def latest_posts(blog: str = "all", limit: int = 10, since: str | None = None) -> list[dict]:
    """Newest posts, newest first.

    Args:
        blog: 'powerbi', 'fabric' or 'all'.
        limit: Max number of posts (1-50).
        since: Optional ISO date (e.g. '2026-10-01'); only posts published on/after it.
    """
    cutoff = _parse_since(since)
    posts = [p for p in await cache.select(blog) if not cutoff or p.published >= cutoff]
    return [p.summary() for p in posts[: max(1, min(limit, 50))]]


@mcp.tool()
async def search_posts(query: str, blog: str = "all", limit: int = 10) -> list[dict]:
    """Search post titles and full text. All words in the query must match (case-insensitive);
    title matches rank higher.

    Args:
        query: Words to search for, e.g. 'copilot semantic model'.
        blog: 'powerbi', 'fabric' or 'all'.
        limit: Max number of results (1-50).
    """
    words = [w for w in query.lower().split() if w]
    scored = []
    for p in await cache.select(blog):
        title, body = p.title.lower(), p.text.lower()
        if all(w in title or w in body for w in words):
            score = sum(3 * title.count(w) + body.count(w) for w in words)
            scored.append((score, p))
    scored.sort(key=lambda sp: (sp[0], sp[1].published), reverse=True)
    return [p.summary() for _, p in scored[: max(1, min(limit, 50))]]


@mcp.tool()
async def get_post(id_or_url: str) -> dict:
    """Full text of a post, by id (e.g. '5375137') or its URL."""
    await cache.refresh()
    match = re.search(r"/ba-p/(\d+)", id_or_url)
    key = match.group(1) if match else id_or_url.strip()
    post = cache.posts.get(key)
    if not post:
        raise ValueError(f"Post '{id_or_url}' not found. Only recent posts from the RSS feeds are available.")
    return {**post.summary(0), "summary": None, "content": post.text}


@mcp.custom_route("/", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "mcp_endpoint": "/mcp", "cached_posts": len(cache.posts)})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
