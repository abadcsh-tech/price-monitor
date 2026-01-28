import feedparser
import re
import ssl
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(
    "https://www.preispirat.ch/feed/",
    headers={"User-Agent": "Mozilla/5.0"},
)
resp = urllib.request.urlopen(req, context=ctx)
feed = feedparser.parse(resp.read())

print(f"Total entries: {len(feed.entries)}\n")

for e in feed.entries:
    t = e.title.lower()
    if "airpods" in t or "ipad" in t or "macbook" in t or "iphone" in t:
        d = e.get("description", "")
        p1 = re.search(r"Preis:\s*CHF\s*([\d'.,]+(?:-)?)", d)
        p2 = re.search(r"Zweitbester\s+Preis:\s*CHF\s*([\d'.,]+(?:-)?)", d)
        print(e.title)
        print(f"  Preis: {p1.group(1) if p1 else 'NONE'}")
        print(f"  Zweitbester: {p2.group(1) if p2 else 'NONE'}")
        print()
