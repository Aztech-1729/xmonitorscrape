import asyncio
import json
import os
from playwright.async_api import async_playwright

COOKIE_FILE = "cookies/session1.json"
TEST_USERNAME = "elonmusk"

async def test_cookies():
    print("=" * 60)
    print("🔧 X/Twitter Cookie Tester")
    print("=" * 60)
    
    # Check if cookie file exists
    if not os.path.exists(COOKIE_FILE):
        print(f"❌ Cookie file not found: {COOKIE_FILE}")
        return
    
    print(f"✅ Cookie file found: {COOKIE_FILE}")
    
    # Load cookies
    try:
        with open(COOKIE_FILE, 'r') as f:
            cookies = json.load(f)
        print(f"✅ Loaded {len(cookies)} cookies")
    except Exception as e:
        print(f"❌ Failed to load cookies: {e}")
        return
    
    print("\n" + "=" * 60)
    print("🌐 Launching browser...")
    print("=" * 60)
    
    playwright = await async_playwright().start()
    
    try:
        # Launch browser
        browser = await playwright.chromium.launch(
            headless=True,  # Headless browser
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage"
            ]
        )
        
        print("✅ Browser launched")
        
        # Create context with cookies
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Add cookies
        await context.add_cookies(cookies)
        print("✅ Cookies loaded into browser")
        
        # Create page
        page = await context.new_page()
        
        print(f"\n🔍 Testing URL: https://x.com/{TEST_USERNAME}")
        print("Please wait...")
        
        # Navigate to test profile
        await page.goto(f"https://x.com/{TEST_USERNAME}", wait_until="domcontentloaded", timeout=15000)
        
        # Wait a bit for page to load
        await asyncio.sleep(3)
        
        # Check if logged in by looking for common elements
        is_logged_in = False
        
        # Check for login redirect
        current_url = page.url
        print(f"\n📍 Current URL: {current_url}")
        
        if "login" in current_url.lower() or "oauth" in current_url.lower():
            print("❌ FAILED: Redirected to login page - cookies are INVALID or EXPIRED")
        else:
            print("✅ SUCCESS: Not redirected to login - cookies appear VALID")
            is_logged_in = True
        
        # Check for profile content
        try:
            tweets = await page.query_selector_all('article[data-testid="tweet"]')
            if tweets:
                print(f"✅ Found {len(tweets)} tweets on the page")
                is_logged_in = True
            else:
                print("⚠️  No tweets found - may need to scroll or cookies might be limited")
        except Exception as e:
            print(f"⚠️  Could not check for tweets: {e}")
        
        # Check for user profile elements
        try:
            profile_exists = await page.query_selector('[data-testid="UserName"]')
            if profile_exists:
                print("✅ Profile loaded successfully")
                is_logged_in = True
        except:
            pass
        
        print("\n" + "=" * 60)
        if is_logged_in:
            print("✅ COOKIE TEST: PASSED")
            print("Your cookies are working correctly!")
        else:
            print("❌ COOKIE TEST: FAILED")
            print("Your cookies may be invalid, expired, or X is blocking them.")
            print("\nSuggestions:")
            print("1. Get fresh cookies from your browser")
            print("2. Make sure you're logged in to X/Twitter")
            print("3. Export cookies using a browser extension")
        print("=" * 60)
        
        print("\n⏳ Waiting 5 seconds before cleanup...")
        await asyncio.sleep(5)
        
        # Cleanup
        await context.close()
        await browser.close()
        await playwright.stop()
        
        print("\n✅ Test completed!")
        
    except Exception as e:
        print(f"\n❌ Error during test: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            await browser.close()
            await playwright.stop()
        except:
            pass

if __name__ == "__main__":
    print("\n🚀 Starting cookie test...\n")
    asyncio.run(test_cookies())
