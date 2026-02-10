# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = "8214352065:AAHssHfOJeZ2hI3zhibeOa6wfOHGUPi_uA4"
ADMIN_USER_IDS = [6670166083, 8370327895]  # Admin Telegram user IDs
BANNER_IMAGE_URL = "https://i.ibb.co/JWHZ7pYH/banner.png"

# Telegram network tuning (VPS-safe)
# Increase timeouts to reduce random NetworkError/httpx.ReadError during long polling
TG_CONNECT_TIMEOUT = 30
TG_READ_TIMEOUT = 60
TG_WRITE_TIMEOUT = 60
TG_POOL_TIMEOUT = 30
TG_REQUEST_CON_POOL_SIZE = 8

# Playwright Browser Settings
BROWSER_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI",
    "--disable-ipc-flooding-protection",
    "--disable-hang-monitor",
    "--disable-client-side-phishing-detection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=user-gesture-required",
    "--disable-background-networking",
    "--disable-default-apps",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-media-session-api"
]

# Scraping Settings
PAGE_POOL_SIZE = 7
MAX_SCROLL_COUNT = 2
SCRAPE_TWEET_LIMIT = 5  # how many latest tweets to parse per profile
SCRAPE_TIMEOUT = 18000  # milliseconds (VPS-safe; heavy profiles can be slow)
TWEET_WAIT_TIMEOUT = 10000  # milliseconds to wait for tweet articles
SCRAPE_RETRIES = 2  # additional recovery retries when tweets aren't found
DEFAULT_POLL_INTERVAL = 90  # seconds (optimized for 50 users)
MAX_MONITORED_USERS = 50

# File Paths
DATA_FILE = "data.json"
COOKIE_FILE = "session1.json"
