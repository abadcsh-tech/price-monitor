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

for i, e in enumerate(feed.entries):
    d = e.get("description", "")
    p1 = re.search(r"Preis:\s*CHF\s*([\d'.,]+(?:-)?)", d)
    p2 = re.search(r"Zweitbester\s+Preis:\s*CHF\s*([\d'.,]+(?:-)?)", d)
    price = p1.group(1) if p1 else "-"
    old = p2.group(1) if p2 else "-"
    print(f"{i+1}. {e.title}")
    print(f"   CHF {price} (alt: {old})")
    print()
