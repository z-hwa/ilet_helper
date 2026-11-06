"""閱讀練習的真實文章來源。

只使用授權明確、允許改編的來源：
- 維基百科（CC BY-SA 4.0）、維基新聞（CC BY 2.5）：官方 API
- Our World in Data（CC BY 4.0）
- NASA（美國政府作品，公有領域）

沒有收錄 BBC：它的 robots.txt 明文禁止爬取，也禁止用在 RAG / grounding，而「抓文章再交給模型改寫出題」正是這種用法。

禮貌原則：帶說明用途的 User-Agent、網頁與 RSS 先檢查 robots.txt、同一網站的請求至少間隔 1 秒。
"""
import asyncio
import os
import random
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import aiohttp
import trafilatura

def contact():
    """維基媒體建議在 User-Agent 附上聯絡方式（信箱或網址）；可在 .env 設定 READING_CONTACT，不設就不附。

    每次使用時才讀環境變數：這個模組比 .env 更早被載入，在 import 時讀會讀到空值。
    """
    return os.getenv("READING_CONTACT", "").strip()


def user_agent():
    extra = f"; {contact()}" if contact() else ""
    return f"ILETSHelperBot/1.0 (personal IELTS practice bot; non-commercial{extra})"
REQUEST_TIMEOUT = 15
SOURCE_TIMEOUT = 25  # 單一來源（含多次請求）最多花多久
MAX_CHARS = 9000  # 交給 Gemini 的來源文字上限（約 1400 字）
MIN_DELAY = 1.0  # 同一網站連續請求的最短間隔（秒）

TOPICS = [
    "renewable energy", "urban planning", "sleep and health", "ocean acidification", "language learning",
    "artificial intelligence", "public transport", "biodiversity loss", "remote work", "nutrition and diet",
    "volcanoes", "coral reefs", "recycling and waste", "space exploration", "climate adaptation",
    "education technology", "migration", "water scarcity", "renewable materials", "animal behaviour",
    "mental health", "robotics", "tourism", "ancient trade routes", "antibiotic resistance",
]
STOP_HEADINGS = {
    "see also", "references", "external links", "further reading", "notes", "sources", "bibliography",
    "citations", "footnotes", "related news", "related articles", "related", "gallery", "explanatory notes",
}


@dataclass
class Article:
    title: str
    text: str
    url: str
    source: str  # 顯示用的來源名稱
    license: str
    credit: str  # 附在題目後面的出處說明

    @property
    def words(self):
        return len(self.text.split())


def clean_text(text):
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def cut_at_headings(text):
    """維基系的純文字摘錄會把章節標題單獨成行；遇到參考資料之類的章節就截斷。"""
    out = []
    for line in text.splitlines():
        if line.strip().strip("=").strip().lower() in STOP_HEADINGS:
            break
        out.append(line)
    return "\n".join(out)


def trim(text, limit=MAX_CHARS):
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip()


class Fetcher:
    """帶禮貌規則的抓取器：robots.txt、同網站請求間隔、逾時。"""

    def __init__(self, session):
        self.session = session
        self._robots = {}
        self._last = {}

    async def _wait(self, url):
        host = urlparse(url).netloc
        gap = time.monotonic() - self._last.get(host, 0)
        if gap < MIN_DELAY:
            await asyncio.sleep(MIN_DELAY - gap)
        self._last[host] = time.monotonic()

    async def _get(self, url, params=None):
        for attempt in range(3):
            await self._wait(url)
            async with self.session.get(
                url, params=params, headers={"User-Agent": user_agent()},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status in (429, 503) and attempt < 2:
                    # 對方要我們慢一點：照 Retry-After 等（上限 8 秒），不硬撞
                    try:
                        wait = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        wait = 3.0 * (attempt + 1)
                    await asyncio.sleep(min(wait, 8.0))
                    continue
                resp.raise_for_status()
                return await resp.text()

    async def json(self, url, params):
        import json

        return json.loads(await self._get(url, params))

    async def allowed(self, url):
        """robots.txt 允許才抓。讀不到 robots.txt（不是 404）時保守起見視為不允許。"""
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                await self._wait(base)
                async with self.session.get(
                    base + "/robots.txt", headers={"User-Agent": user_agent()},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 404:
                        parser.parse([])  # 沒有 robots.txt = 沒有限制
                    elif resp.status == 200:
                        parser.parse((await resp.text()).splitlines())
                    else:
                        parser.disallow_all = True
            except Exception:
                parser.disallow_all = True
            self._robots[base] = parser
        return self._robots[base].can_fetch(user_agent(), url)

    async def page(self, url):
        if not await self.allowed(url):
            raise PermissionError(f"robots.txt 不允許抓取 {url}")
        return await self._get(url)


def matches(topic, *fields):
    if not topic:
        return True
    words = [w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2]
    haystack = " ".join(f or "" for f in fields).lower()
    return any(w in haystack for w in words) if words else True


# ---------------- 各來源 ----------------
WIKI_API = "https://en.wikipedia.org/w/api.php"
NEWS_API = "https://en.wikinews.org/w/api.php"


async def wikipedia(f, topic, used):
    query = topic or random.choice(TOPICS)
    data = await f.json(WIKI_API, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": 15, "srnamespace": 0,
        "srprop": "wordcount|snippet", "format": "json",
    })
    hits = [
        h for h in data.get("query", {}).get("search", [])
        if h.get("wordcount", 0) >= 700
        and not h["title"].startswith(("List of", "Index of", "Outline of", "Timeline of"))
        and "may refer to" not in h.get("snippet", "")
    ]
    random.shuffle(hits)
    for hit in hits[:4]:
        title = hit["title"]
        url = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
        if url in used:
            continue
        ext = await f.json(WIKI_API, {
            "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "plain",
            "titles": title, "redirects": 1, "format": "json",
        })
        page = next(iter(ext.get("query", {}).get("pages", {}).values()), {})
        text = trim(cut_at_headings(page.get("extract", "")))
        if len(text.split()) >= 250:
            return Article(title, text, url, "維基百科", "CC BY-SA 4.0",
                           f"維基百科「{title}」，CC BY-SA 4.0（作者見頁面歷史）")
    return None


async def wikinews(f, topic, used):
    if topic:
        data = await f.json(NEWS_API, {"action": "query", "list": "search", "srsearch": topic,
                                       "srlimit": 15, "srnamespace": 0, "format": "json"})
        titles = [h["title"] for h in data.get("query", {}).get("search", [])]
    else:
        data = await f.json(NEWS_API, {"action": "query", "list": "categorymembers", "cmtitle": "Category:Published",
                                       "cmlimit": 200, "cmtype": "page", "format": "json"})
        titles = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
    random.shuffle(titles)
    for title in titles[:5]:
        url = "https://en.wikinews.org/wiki/" + quote(title.replace(" ", "_"))
        if url in used:
            continue
        ext = await f.json(NEWS_API, {
            "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "plain",
            "titles": title, "redirects": 1, "format": "json",
        })
        page = next(iter(ext.get("query", {}).get("pages", {}).values()), {})
        text = trim(cut_at_headings(page.get("extract", "")))
        if len(text.split()) >= 150:
            return Article(title, text, url, "維基新聞", "CC BY 4.0", f"維基新聞「{title}」，CC BY 4.0")
    return None


VOYAGE_API = "https://en.wikivoyage.org/w/api.php"
DESTINATIONS = [
    "Kyoto", "Iceland", "Patagonia", "Lisbon", "Marrakech", "Vietnam", "Scotland", "Cape Town", "Istanbul",
    "Peru", "New Zealand", "Norway", "Bali", "Morocco", "Tasmania", "Hokkaido", "Slovenia", "Jordan",
    "Backpacking", "Cruising", "Train travel", "Ecotourism", "National parks",
]
# 維基導遊的標準章節：實用資訊（交通、住宿、治安…）不適合當閱讀文章，只留介紹、景點、活動、飲食等敘述性章節
VOYAGE_DROP = {
    "get in", "get around", "buy", "sleep", "connect", "stay safe", "stay healthy", "respect", "cope", "go next",
    "districts", "cities", "other destinations", "regions", "by plane", "by train", "by car", "by bus", "by boat",
    "by foot", "by bike", "contact", "fees and permits", "stay healthy",
}
VOYAGE_KEEP = {"understand", "see", "do", "eat", "drink", "history", "climate", "culture", "talk", "landscape",
                "orientation", "itineraries", "events", "festivals", "wildlife", "geography", "people"}


def voyage_prose(text):
    """只留維基導遊的敘述性章節，並濾掉電話、網址、價格這類清單式資訊行。"""
    out, dropping = [], False
    for line in text.splitlines():
        heading = line.strip().lower()
        if heading in VOYAGE_DROP:
            dropping = True
            continue
        if heading in VOYAGE_KEEP:
            dropping = False
            continue
        if dropping or not line.strip():
            if not dropping:
                out.append("")
            continue
        digits = sum(c.isdigit() for c in line)
        if "http" in line or "+" in line and digits > 6 or digits > len(line) * 0.15:
            continue  # 電話、網址、價格、地址
        out.append(line)
    return clean_text("\n".join(out))


async def wikivoyage(f, topic, used):
    """維基導遊：旅遊目的地與旅遊主題的介紹（CC BY-SA 4.0）。"""
    query = topic or random.choice(DESTINATIONS)
    data = await f.json(VOYAGE_API, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": 15, "srnamespace": 0,
        "srprop": "wordcount|snippet", "format": "json",
    })
    hits = [
        h for h in data.get("query", {}).get("search", [])
        if h.get("wordcount", 0) >= 900 and not h["title"].startswith(("Phrasebook", "Wikivoyage"))
    ]
    random.shuffle(hits)
    for hit in hits[:4]:
        title = hit["title"]
        url = "https://en.wikivoyage.org/wiki/" + quote(title.replace(" ", "_"))
        if url in used:
            continue
        ext = await f.json(VOYAGE_API, {
            "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "plain",
            "titles": title, "redirects": 1, "format": "json",
        })
        page = next(iter(ext.get("query", {}).get("pages", {}).values()), {})
        text = trim(voyage_prose(cut_at_headings(page.get("extract", ""))))
        if len(text.split()) >= 250:
            return Article(title, text, url, "維基導遊", "CC BY-SA 4.0", f"維基導遊 Wikivoyage「{title}」，CC BY-SA 4.0")
    return None


def _extract_html(html):
    return trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""


async def owid(f, topic, used):
    feed = await f.page("https://ourworldindata.org/atom.xml")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []
    for e in ET.fromstring(feed).findall("a:entry", ns):
        link = e.find("a:link", ns)
        title = (e.findtext("a:title", "", ns) or "").strip()
        if link is not None and matches(topic, title, e.findtext("a:summary", "", ns)):
            entries.append((title, link.get("href")))
    random.shuffle(entries)
    for title, url in entries[:4]:
        if url in used:
            continue
        html = await f.page(url)
        text = trim(await asyncio.to_thread(_extract_html, html))
        if len(text.split()) >= 300:
            return Article(title, text, url, "Our World in Data", "CC BY 4.0",
                           f"Our World in Data「{title}」，CC BY 4.0")
    return None


async def nasa(f, topic, used):
    feed = await f.page("https://www.nasa.gov/feed/")
    ns = {"c": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    for item in ET.fromstring(feed).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        html = item.findtext("c:encoded", "", ns) or item.findtext("description") or ""
        if link and link not in used and matches(topic, title, html[:500]):
            items.append((title, link, html))
    random.shuffle(items)
    for title, link, html in items[:6]:
        text = trim(await asyncio.to_thread(_extract_html, f"<html><body>{html}</body></html>"))
        if len(text.split()) >= 250:
            return Article(title, text, link, "NASA", "公有領域（美國政府作品）", f"NASA「{title}」（美國政府作品，公有領域）")
    return None


async def nist(f, topic, used):
    feed = await f.page("https://www.nist.gov/news-events/news/rss.xml")
    items = []
    for item in ET.fromstring(feed).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if link and link not in used and matches(topic, title, item.findtext("description")):
            items.append((title, link))
    random.shuffle(items)
    for title, link in items[:4]:
        html = await f.page(link)
        text = trim(await asyncio.to_thread(_extract_html, html))
        if len(text.split()) >= 250:
            return Article(title, text, link, "NIST", "公有領域（美國政府作品）", f"NIST「{title}」（美國政府作品，公有領域）")
    return None


async def globalvoices(f, topic, used):
    """Global Voices：各國公民記者寫的新聞、文化、旅遊與社會議題，內文為 CC BY 3.0，RSS 直接附全文。"""
    feed = await f.page("https://globalvoices.org/feed/")
    ns = {"c": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    for item in ET.fromstring(feed).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        categories = " ".join(c.text or "" for c in item.findall("category"))
        html = item.findtext("c:encoded", "", ns)
        if link and link not in used and matches(topic, title, categories):
            items.append((title, link, html))
    random.shuffle(items)
    for title, link, html in items[:6]:
        text = trim(await asyncio.to_thread(_extract_html, f"<html><body>{html}</body></html>"))
        if len(text.split()) >= 300:
            return Article(title, text, link, "Global Voices", "CC BY 3.0", f"Global Voices「{title}」，CC BY 3.0")
    return None


async def voa(f, topic, used):
    """VOA Learning English：為英文學習者寫的新聞與專題（美國政府製作的內容屬公有領域，部分第三方素材除外）。"""
    feed = await f.page("https://learningenglish.voanews.com/api/")
    items = []
    for item in ET.fromstring(feed).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if link and link not in used and matches(topic, title, item.findtext("description")):
            items.append((title, link))
    random.shuffle(items)
    for title, link in items[:8]:  # 這個來源有不少影音頁，字數不夠的會被略過
        html = await f.page(link)
        text = trim(await asyncio.to_thread(_extract_html, html))
        if len(text.split()) >= 250:
            return Article(title, text, link, "VOA Learning English", "公有領域（美國政府作品）",
                           f"VOA Learning English「{title}」（美國政府作品，公有領域）")
    return None


SOURCES = {
    "wikipedia": wikipedia, "wikinews": wikinews, "owid": owid, "nasa": nasa, "nist": nist,
    "globalvoices": globalvoices, "voa": voa, "wikivoyage": wikivoyage,
}
# 維基媒體對沒有聯絡方式的程式限流很嚴，所以沒設 READING_CONTACT 時不使用
NEEDS_CONTACT = {"wikipedia", "wikinews", "wikivoyage"}


def available_sources(enabled):
    """目前實際可用的來源（維基系需要先設定聯絡方式）。"""
    return [n for n in enabled if n in SOURCES and (contact() or n not in NEEDS_CONTACT)]


async def find_article(topic, min_words, enabled, used):
    """依隨機順序逐一嘗試來源，直到找到夠長的文章；某個來源失敗就換下一個。

    回傳 (文章或 None, 失敗紀錄)。都達不到 min_words 時，回傳其中最長的一篇。
    """
    names = available_sources(enabled)
    random.shuffle(names)
    failures, candidates = [], []
    async with aiohttp.ClientSession() as session:
        fetcher = Fetcher(session)
        for name in names:
            try:
                article = await asyncio.wait_for(SOURCES[name](fetcher, topic, used), SOURCE_TIMEOUT)
            except Exception as e:
                failures.append((name, f"{type(e).__name__}: {str(e)[:80]}"))
                continue
            if article is None:
                failures.append((name, "找不到符合條件的文章"))
                continue
            if article.words >= min_words:
                return article, failures
            candidates.append(article)
    return (max(candidates, key=lambda a: a.words) if candidates else None), failures
