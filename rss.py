"""google_rss_search: recent news on a topic via Google News RSS.

The feed returns headline, publisher and date -- not article text. So anything a draft cites
from here is limited to what the headline itself says, and the draft links the item so Meera can
open it before posting.

Note: Google's feed terms describe it as for personal, non-commercial feed-reader use.
"""
import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEED = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
MAX_RESULTS = 10
RECENCY = "when:180d"


def google_rss_search(query):
    q = urllib.parse.quote_plus(f"{query} {RECENCY}")
    req = urllib.request.Request(FEED.format(q=q), headers={"User-Agent": "Mozilla/5.0 content-desk"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    results = []
    for item in root.findall(".//item")[:MAX_RESULTS]:
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        title = html.unescape(item.findtext("title") or "").strip()
        if source and title.endswith(" - " + source):  # Google appends " - Publisher" to titles
            title = title[: -len(source) - 3]
        try:
            published = parsedate_to_datetime(item.findtext("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            published = ""
        results.append({
            "title": re.sub(r"\s+", " ", title),
            "source": source,
            "published": published,
            "url": item.findtext("link") or "",
        })
    return results
