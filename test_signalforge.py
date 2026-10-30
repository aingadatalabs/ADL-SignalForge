import asyncio
import frankensurf as fs

async def test_run():
    # Test with a sample URL or an agency site you want to qualify
    test_url = "https://youtube.com"
    
    print(f"Testing Frankensurf read on: {test_url}")
    page = await fs.aread(test_url)
    
    if page.ok:
        print("\n--- Success ---")
        print(f"Title: {page.title}")
        print(f"Content Length: {len(page.text)} characters")
        print(f"Receipt (Tool used): {page.receipt}")
    else:
        print("\n--- Failed ---")
        print(f"Error code: {page.failure}")

if __name__ == "__main__":
    asyncio.run(test_run())