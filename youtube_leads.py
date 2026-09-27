import json
from scrapling import StealthyFetcher

fetcher = StealthyFetcher()
search_url = "https://www.youtube.com/results?search_query=tech+reviews&sp=EgIQAg%253D%253D"
page = fetcher.fetch(search_url, real_chrome=True)

print(f"Status: {page.status}")

channels = page.css("ytd-channel-renderer")
results = []

for ch in channels:
    title = ch.css("#text::text").first
    link = ch.css("#main-link::attr(href)").first
    if title and link:
        results.append({
            "name": title.text.strip(),
            "url": f"https://www.youtube.com{link.text.strip()}/about"
        })

print(f"Found {len(results)} channel leads:")
print(json.dumps(results[:5], indent=2))
