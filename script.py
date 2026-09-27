from scrapling import StealthyFetcher

fetcher = StealthyFetcher()
page = fetcher.fetch("https://httpbin.org/headers", real_chrome=True)

print(f"Status Code: {page.status}")
print("Response Text:")
print(page.text)

