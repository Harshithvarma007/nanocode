"""web_fetch: read a web page as text the model can use.

HTML is turned into compact Markdown-ish text (headings, lists, code blocks, links) with the
standard library's HTMLParser - no browser, no extra dependency. Output is capped to the
context budget like every other tool result.
"""

from __future__ import annotations

import re
import threading
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from . import __version__

MAX_BYTES = 5_000_000
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript", "application/x-yaml")


class _Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "nav", "footer", "form", "button"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "table", "tr", "blockquote", "ul", "ol", "dl",
             "dd", "dt", "figure", "figcaption", "hr", "br"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.title = base_url, [], ""
        self.skip = self.pre = 0
        self.in_title = False
        self.href: str | None = None
        self.link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":  # lives inside <head>, which is otherwise skipped
            self.in_title = True
            return
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:")):
                self.href, self.link_text = urljoin(self.base, href), []
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a" and self.href:
            text = "".join(self.link_text).strip()
            if text and text != self.href:
                self.out.append(f" ({self.href})")
            self.href = None
        elif tag in self.BLOCK or tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if self.href is not None:
            self.link_text.append(text)
        self.out.append(text)


def html_to_text(html: str, base_url: str) -> tuple[str, str]:
    parser = _Extractor(base_url)
    parser.feed(html)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return " ".join(parser.title.split()), text


def fetch(url: str, limit: int, cancel: threading.Event | None = None) -> tuple[str, str]:
    """Returns (text for the model, one-line summary). Raises ToolError on failure."""
    from .tools import ToolError, truncate_middle

    if urlparse(url).scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs can be fetched.")
    headers = {"User-Agent": f"nanocode/{__version__} (web_fetch)", "Accept": "text/html,text/plain,*/*;q=0.8"}
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0), headers=headers) as client:
            with client.stream("GET", url) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if cancel is not None and cancel.is_set():
                        raise KeyboardInterrupt
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                body = b"".join(chunks)
                status, final_url = response.status_code, str(response.url)
                content_type = response.headers.get("content-type", "").lower()
                encoding = response.encoding or "utf-8"
    except httpx.HTTPError as e:
        raise ToolError(f"Couldn't fetch {url}: {e}") from e

    raw = body.decode(encoding, errors="replace")
    if "html" in content_type or raw.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        title, text = html_to_text(raw, final_url)
    elif any(content_type.startswith(t) for t in TEXT_TYPES) or not content_type:
        title, text = "", raw
    else:
        raise ToolError(f"{url} returned {content_type or 'binary data'}, which can't be read as text.")

    header = f"URL: {final_url}\nStatus: {status}" + (f"\nTitle: {title}" if title else "")
    out = header + "\n\n" + truncate_middle(text, limit)
    summary = f"{status} · {len(text):,} chars" + (f" · {title[:60]}" if title else "")
    if status >= 400:
        summary = f"HTTP {status}" + (f" · {title[:60]}" if title else "")
    return out, summary
