import asyncio
import json
import os
import sys
import tempfile
import warnings
from datetime import datetime
from typing import Dict, List, Optional, Set

# Suppress warnings BEFORE importing telegram libraries
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', message='.*pkg_resources.*')

from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
import config


class DataManager:
    def __init__(self, filepath: str = config.DATA_FILE):
        self.filepath = filepath
        self._lock = asyncio.Lock()
        self._ensure_file_exists()
    
    def _ensure_file_exists(self):
        if not os.path.exists(self.filepath):
            self._atomic_write({
                "settings": {"poll_seconds": 120, "monitoring_active": False},
                "targets": [],
                "keywords": [],
                "last_seen": {}
            })
    
    def _atomic_write(self, data: dict):
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.filepath), text=True)
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.filepath)
        except:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
    
    async def read(self) -> dict:
        async with self._lock:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    
    async def write(self, data: dict):
        async with self._lock:
            self._atomic_write(data)
    
    async def add_target(self, username: str) -> bool:
        data = await self.read()
        username = username.lstrip('@').lower()
        if username not in data['targets'] and len(data['targets']) < config.MAX_MONITORED_USERS:
            data['targets'].append(username)
            await self.write(data)
            return True
        return False
    
    async def remove_target(self, username: str) -> bool:
        data = await self.read()
        username = username.lstrip('@').lower()
        if username in data['targets']:
            data['targets'].remove(username)
            if username in data['last_seen']:
                del data['last_seen'][username]
            await self.write(data)
            return True
        return False
    
    async def update_last_seen(self, username: str, tweet_id: str):
        data = await self.read()
        data['last_seen'][username] = tweet_id
        await self.write(data)
    
    async def set_monitoring(self, active: bool):
        data = await self.read()
        data['settings']['monitoring_active'] = active
        await self.write(data)
    
    async def set_poll_interval(self, seconds: int):
        data = await self.read()
        data['settings']['poll_seconds'] = seconds
        await self.write(data)


class PagePool:
    def __init__(self, context: BrowserContext, size: int):
        self.context = context
        self.size = size
        self.semaphore = asyncio.Semaphore(size)
        self.pages: List[Page] = []
        self.available: asyncio.Queue = asyncio.Queue()
        self._initialized = False

    async def refresh_all_pages(self):
        """Soft-refresh all pooled pages between monitoring cycles.

        This helps on VPS where X sometimes stops rendering tweets after many navigations.
        We keep it lightweight by navigating to about:blank.
        """
        async def _refresh(page: Page):
            try:
                await page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
            except Exception:
                # Ignore refresh errors; page will be reused anyway
                pass

        await asyncio.gather(*[_refresh(p) for p in self.pages], return_exceptions=True)
    
    async def initialize(self):
        if self._initialized:
            return
        
        for _ in range(self.size):
            page = await self.context.new_page()
            await page.set_viewport_size({"width": 1280, "height": 720})
            self.pages.append(page)
            await self.available.put(page)
        
        self._initialized = True
    
    async def acquire(self) -> Page:
        await self.semaphore.acquire()
        return await self.available.get()
    
    async def release(self, page: Page):
        await self.available.put(page)
        self.semaphore.release()
    
    async def cleanup(self):
        for page in self.pages:
            try:
                await page.close()
            except:
                pass
        self.pages.clear()


class TwitterScraper:
    def __init__(self, page_pool: PagePool):
        self.page_pool = page_pool
    
    async def scrape_user(self, username: str) -> List[Dict]:
        page = await self.page_pool.acquire()
        try:
            url = f"https://x.com/{username}"

            async def load_and_wait_for_tweets() -> bool:
                # VPS/slow networks: navigation + rendering can exceed SCRAPE_TIMEOUT.
                await page.goto(url, wait_until="domcontentloaded", timeout=max(15000, config.SCRAPE_TIMEOUT))
                # Give the client-side app time to render
                await asyncio.sleep(1.5)
                try:
                    await page.wait_for_selector('article[data-testid="tweet"]', timeout=6000)
                    return True
                except Exception:
                    return False

            loaded = await load_and_wait_for_tweets()

            if not loaded:
                # Retry once (common on VPS due to delayed rendering / transient throttling)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=max(15000, config.SCRAPE_TIMEOUT))
                except Exception:
                    # If reload fails, try one more goto
                    await page.goto(url, wait_until="domcontentloaded", timeout=max(15000, config.SCRAPE_TIMEOUT))

                await asyncio.sleep(2)
                try:
                    await page.wait_for_selector('article[data-testid="tweet"]', timeout=6000)
                except Exception:
                    pass

            # Small scroll to trigger lazy load (even if tweets exist)
            for _ in range(config.MAX_SCROLL_COUNT):
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(0.8)

            tweets = await page.evaluate("""
                () => {
                    const results = [];
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    
                    for (let i = 0; i < Math.min(3, articles.length); i++) {
                        const article = articles[i];
                        try {
                            const tweetData = {
                                text: '',
                                images: [],
                                videos: [],
                                link: '',
                                tweet_id: '',
                                is_retweet: false,
                                original_author: '',
                                timestamp: Date.now()
                            };
                            
                            // Check for retweet/repost - improved detection
                            const socialContext = article.querySelector('[data-testid="socialContext"]');
                            if (socialContext) {
                                const contextText = socialContext.textContent.toLowerCase();
                                if (contextText.includes('repost') || contextText.includes('retweet')) {
                                    tweetData.is_retweet = true;
                                }
                            }
                            
                            // Get tweet text
                            const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
                            if (tweetTextEl) {
                                tweetData.text = tweetTextEl.innerText;
                            }
                            
                            // Get author - improved for retweets/reposts
                            if (tweetData.is_retweet) {
                                const userNameSection = article.querySelector('[data-testid="User-Name"]');
                                if (userNameSection) {
                                    const authorLink = userNameSection.querySelector('a[role="link"][href^="/"]');
                                    if (authorLink) {
                                        const href = authorLink.getAttribute('href');
                                        if (href && href !== '/' && !href.includes('/status/')) {
                                            tweetData.original_author = href.replace('/', '').split('/')[0];
                                        }
                                    }
                                }
                            }
                            
                            // Get tweet link and ID
                            const timeEl = article.querySelector('time');
                            if (timeEl) {
                                const link = timeEl.closest('a');
                                if (link) {
                                    const href = link.getAttribute('href');
                                    if (href) {
                                        tweetData.link = 'https://x.com' + href;
                                        const parts = href.split('/');
                                        const statusIdx = parts.indexOf('status');
                                        if (statusIdx !== -1 && parts.length > statusIdx + 1) {
                                            tweetData.tweet_id = parts[statusIdx + 1];
                                        }
                                    }
                                }
                            }
                            
                            // Get images
                            const imgEls = article.querySelectorAll('[data-testid="tweetPhoto"] img');
                            imgEls.forEach(img => {
                                const src = img.getAttribute('src');
                                if (src && src.includes('pbs.twimg.com')) {
                                    tweetData.images.push(src);
                                }
                            });
                            
                            // Get videos - improved with multiple strategies
                            const videoEls = article.querySelectorAll('video');
                            videoEls.forEach(video => {
                                let videoUrl = null;
                                
                                // Strategy 1: Direct src attribute
                                const directSrc = video.getAttribute('src');
                                if (directSrc && !directSrc.startsWith('blob:')) {
                                    videoUrl = directSrc;
                                }
                                
                                // Strategy 2: Source element
                                if (!videoUrl) {
                                    const source = video.querySelector('source');
                                    if (source) {
                                        const sourceSrc = source.getAttribute('src');
                                        if (sourceSrc && !sourceSrc.startsWith('blob:')) {
                                            videoUrl = sourceSrc;
                                        }
                                    }
                                }
                                
                                // Strategy 3: Poster attribute as fallback indicator
                                if (!videoUrl) {
                                    const poster = video.getAttribute('poster');
                                    if (poster && poster.includes('pbs.twimg.com')) {
                                        // Video exists but URL not accessible via DOM
                                        // Tweet link preview will handle it
                                    }
                                }
                                
                                if (videoUrl) {
                                    tweetData.videos.push(videoUrl);
                                }
                            });
                            
                            if (tweetData.tweet_id) {
                                results.push(tweetData);
                            }
                        } catch (e) {
                            console.error('Error parsing tweet:', e);
                        }
                    }
                    
                    return results;
                }
            """)
            
            if tweets:
                return tweets

            # If still no tweets, do one last soft refresh + re-check quickly.
            try:
                await page.reload(wait_until="domcontentloaded", timeout=max(15000, config.SCRAPE_TIMEOUT))
                await asyncio.sleep(2)
                try:
                    await page.wait_for_selector('article[data-testid="tweet"]', timeout=5000)
                except Exception:
                    pass

                tweets = await page.evaluate("""
                    () => {
                        const results = [];
                        const articles = document.querySelectorAll('article[data-testid="tweet"]');
                        for (let i = 0; i < Math.min(3, articles.length); i++) {
                            const article = articles[i];
                            try {
                                const tweetData = {
                                    text: '',
                                    images: [],
                                    videos: [],
                                    link: '',
                                    tweet_id: '',
                                    is_retweet: false,
                                    original_author: '',
                                    timestamp: Date.now()
                                };

                                const socialContext = article.querySelector('[data-testid="socialContext"]');
                                if (socialContext) {
                                    const contextText = socialContext.textContent.toLowerCase();
                                    if (contextText.includes('repost') || contextText.includes('retweet')) {
                                        tweetData.is_retweet = true;
                                    }
                                }

                                const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
                                if (tweetTextEl) {
                                    tweetData.text = tweetTextEl.innerText;
                                }

                                if (tweetData.is_retweet) {
                                    const userNameSection = article.querySelector('[data-testid="User-Name"]');
                                    if (userNameSection) {
                                        const authorLink = userNameSection.querySelector('a[role="link"][href^="/"]');
                                        if (authorLink) {
                                            const href = authorLink.getAttribute('href');
                                            if (href && href !== '/' && !href.includes('/status/')) {
                                                tweetData.original_author = href.replace('/', '').split('/')[0];
                                            }
                                        }
                                    }
                                }

                                const timeEl = article.querySelector('time');
                                if (timeEl) {
                                    const link = timeEl.closest('a');
                                    if (link) {
                                        const href = link.getAttribute('href');
                                        if (href) {
                                            tweetData.link = 'https://x.com' + href;
                                            const parts = href.split('/');
                                            const statusIdx = parts.indexOf('status');
                                            if (statusIdx !== -1 && parts.length > statusIdx + 1) {
                                                tweetData.tweet_id = parts[statusIdx + 1];
                                            }
                                        }
                                    }
                                }

                                const imgEls = article.querySelectorAll('[data-testid="tweetPhoto"] img');
                                imgEls.forEach(img => {
                                    const src = img.getAttribute('src');
                                    if (src && src.includes('pbs.twimg.com')) {
                                        tweetData.images.push(src);
                                    }
                                });

                                const videoEls = article.querySelectorAll('video');
                                videoEls.forEach(video => {
                                    let videoUrl = null;
                                    const directSrc = video.getAttribute('src');
                                    if (directSrc && !directSrc.startsWith('blob:')) {
                                        videoUrl = directSrc;
                                    }
                                    if (!videoUrl) {
                                        const source = video.querySelector('source');
                                        if (source) {
                                            const sourceSrc = source.getAttribute('src');
                                            if (sourceSrc && !sourceSrc.startsWith('blob:')) {
                                                videoUrl = sourceSrc;
                                            }
                                        }
                                    }
                                    if (videoUrl) {
                                        tweetData.videos.push(videoUrl);
                                    }
                                });

                                if (tweetData.tweet_id) {
                                    results.push(tweetData);
                                }
                            } catch (e) {}
                        }
                        return results;
                    }
                """);

                return tweets or []
            except Exception:
                return []

        except Exception as e:
            print(f"Error scraping {username}: {e}")
            return []
        finally:
            await self.page_pool.release(page)


class MonitoringService:
    def __init__(self, data_manager: DataManager, scraper: TwitterScraper, telegram_app):
        self.data_manager = data_manager
        self.scraper = scraper
        self.telegram_app = telegram_app
        self.monitoring_task: Optional[asyncio.Task] = None
        self.running = False
    
    async def start(self):
        if self.running:
            return
        
        self.running = True
        await self.data_manager.set_monitoring(True)
        self.monitoring_task = asyncio.create_task(self._monitor_loop())
    
    async def stop(self):
        self.running = False
        await self.data_manager.set_monitoring(False)
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
    
    async def _process_user(self, username: str):
        try:
            print(f"🔍 Checking @{username}...")
            tweets = await self.scraper.scrape_user(username)

            if not tweets:
                print(
                    f"⚠️  No tweets found for @{username} "
                    "(slow load / protected account / rate limit / selector change)"
                )
                return

            # tweets are returned newest->older (top of timeline first)
            print(f"✅ Found {len(tweets)} tweet(s) for @{username}")

            data = await self.data_manager.read()
            last_seen = data['last_seen'].get(username)

            if last_seen is None:
                # Baseline: set latest tweet id, do not send old tweets
                latest_id = tweets[0]['tweet_id']
                print(f"   📝 First time monitoring @{username}, setting baseline to {latest_id}")
                await self.data_manager.update_last_seen(username, latest_id)
                return

            # Collect all tweets newer than last_seen (within our scraped window)
            new_tweets: List[dict] = []
            for t in tweets:
                if t.get('tweet_id') == last_seen:
                    break
                new_tweets.append(t)

            if not new_tweets:
                print(f"   ✓ No new tweets for @{username}")
                return

            # Send in chronological order (oldest -> newest)
            print(f"   📢 {len(new_tweets)} new tweet(s) detected for @{username}")
            for t in reversed(new_tweets):
                if self._should_notify(t, data['keywords']):
                    await self._send_tweet_to_telegram(username, t)
                else:
                    print(f"   ⏭️  Tweet filtered by keywords")

            # Update last_seen to the newest tweet id
            newest_id = new_tweets[0]['tweet_id']
            await self.data_manager.update_last_seen(username, newest_id)

        except Exception as e:
            print(f"❌ Error monitoring {username}: {e}")
            import traceback
            traceback.print_exc()
    
    async def _monitor_loop(self):
        print("🔄 Monitoring service started!")
        while self.running:
            try:
                data = await self.data_manager.read()
                targets = data['targets']
                poll_interval = data['settings']['poll_seconds']
                
                if not targets:
                    print("⏸️  No usernames to monitor. Waiting...")
                    await asyncio.sleep(10)
                    continue
                
                print(f"\n{'='*60}")
                print(f"🔄 Starting monitoring cycle for {len(targets)} user(s)")
                print(f"{'='*60}")
                
                tasks = [self._process_user(username) for username in targets]
                await asyncio.gather(*tasks, return_exceptions=True)

                # Refresh pooled pages between cycles to reduce "No tweets found" on VPS
                try:
                    await self.scraper.page_pool.refresh_all_pages()
                except Exception:
                    pass
                
                print(f"\n✅ Cycle complete. Next check in {poll_interval} seconds...")
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                print(f"❌ Monitor loop error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(10)
    
    def _should_notify(self, tweet: dict, keywords: List[str]) -> bool:
        if not keywords:
            return True
        
        text = tweet['text'].lower()
        return any(kw.lower() in text for kw in keywords)
    
    async def _send_tweet_to_telegram(self, username: str, tweet: dict):
        try:
            data = await self.data_manager.read()
            admin_ids = config.ADMIN_USER_IDS
            
            if tweet['is_retweet']:
                message = f"🔁 <b>Retweeted by @{username}</b>\n\n"
                if tweet['original_author']:
                    message += f"👤 <b>Original:</b> @{tweet['original_author']}\n\n"
            else:
                message = f"📢 <b>@{username}</b>\n\n"
            
            if tweet['text']:
                message += f"📝 {tweet['text']}\n\n"
            
            if tweet['images']:
                message += f"🖼️ <b>Images:</b> {len(tweet['images'])}\n"
                for img in tweet['images'][:4]:
                    message += f"  • {img}\n"
                message += "\n"
            
            if tweet['videos']:
                message += f"🎥 <b>Videos:</b> {len(tweet['videos'])}\n"
                for vid in tweet['videos'][:2]:
                    message += f"  • {vid}\n"
                message += "\n"
            
            if tweet['link']:
                message += f"🔗 <a href='{tweet['link']}'>View Tweet</a>"
            
            for admin_id in admin_ids:
                try:
                    await self.telegram_app.bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )
                except Exception as e:
                    print(f"Failed to send to {admin_id}: {e}")
        
        except Exception as e:
            print(f"Error sending tweet notification: {e}")


class TelegramBot:
    def __init__(self):
        self.data_manager = DataManager()
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page_pool: Optional[PagePool] = None
        self.scraper: Optional[TwitterScraper] = None
        self.monitoring_service: Optional[MonitoringService] = None
        self.app: Optional[Application] = None

    def _build_app(self) -> Application:
        # VPS-safe network tuning for Telegram long polling
        request = HTTPXRequest(
            connect_timeout=getattr(config, "TG_CONNECT_TIMEOUT", 30),
            read_timeout=getattr(config, "TG_READ_TIMEOUT", 60),
            write_timeout=getattr(config, "TG_WRITE_TIMEOUT", 60),
            pool_timeout=getattr(config, "TG_POOL_TIMEOUT", 30),
            connection_pool_size=getattr(config, "TG_REQUEST_CON_POOL_SIZE", 8),
        )

        app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .request(request)
            .build()
        )

        app.add_error_handler(self.error_handler)
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("menu", self.cmd_menu))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Let PTB own the event loop and call our async hooks
        app.post_init = self.on_startup
        app.post_shutdown = self.on_shutdown

        return app

    async def on_startup(self, app: Application):
        """PTB startup hook: initialize Playwright & monitoring services."""
        self.app = app

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=config.BROWSER_ARGS,
        )

        cookies = []
        if os.path.exists(config.COOKIE_FILE):
            try:
                with open(config.COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
            except Exception as e:
                print(f"⚠️ Failed to read cookies: {e}")

        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        if cookies:
            await self.context.add_cookies(cookies)

        self.page_pool = PagePool(self.context, config.PAGE_POOL_SIZE)
        await self.page_pool.initialize()

        self.scraper = TwitterScraper(self.page_pool)
        self.monitoring_service = MonitoringService(self.data_manager, self.scraper, app)

        data = await self.data_manager.read()
        if data.get("settings", {}).get("monitoring_active"):
            await self.monitoring_service.start()

    async def on_shutdown(self, app: Application):
        """PTB shutdown hook: cleanup Playwright resources."""
        await self.cleanup()
    
    def _is_admin(self, user_id: int) -> bool:
        return user_id in config.ADMIN_USER_IDS
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors in the telegram bot."""
        import traceback
        print(f"⚠️ Error: {context.error}")
        traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        
        try:
            await update.message.reply_photo(
                photo=config.BANNER_IMAGE_URL,
                caption="🤖 <b>X Monitor Bot</b>\n\n"
                        "Welcome! Use /menu to access controls.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"Error in cmd_start: {e}")
            await update.message.reply_text(
                "🤖 <b>X Monitor Bot</b>\n\n"
                "Welcome! Use /menu to access controls.",
                parse_mode=ParseMode.HTML
            )
    
    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        
        await self._show_main_menu(update.effective_chat.id)
    
    async def _show_main_menu(self, chat_id: int):
        data = await self.data_manager.read()
        status = "🟢 Active" if data['settings']['monitoring_active'] else "🔴 Stopped"
        
        keyboard = [
            [InlineKeyboardButton(f"📊 Status: {status}", callback_data="status")],
            [InlineKeyboardButton("➕ Add Username", callback_data="add_user")],
            [InlineKeyboardButton("➖ Remove Username", callback_data="remove_user")],
            [InlineKeyboardButton("📋 List Usernames", callback_data="list_users")],
            [InlineKeyboardButton("▶️ Start Monitoring", callback_data="start_monitor")],
            [InlineKeyboardButton("⏸️ Stop Monitoring", callback_data="stop_monitor")]
        ]
        
        try:
            await self.app.bot.send_photo(
                chat_id=chat_id,
                photo=config.BANNER_IMAGE_URL,
                caption="🎛️ <b>Control Panel</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"Error in _show_main_menu: {e}")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="🎛️ <b>Control Panel</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if not self._is_admin(query.from_user.id):
            return
        
        action = query.data
        
        if action == "status":
            await self._show_status(query.message.chat_id)
        elif action == "add_user":
            context.user_data['awaiting'] = 'username_add'
            await query.message.reply_text("Send the X username to monitor (without @):")
        elif action == "remove_user":
            context.user_data['awaiting'] = 'username_remove'
            await query.message.reply_text("Send the X username to remove (without @):")
        elif action == "list_users":
            await self._list_users(query.message.chat_id)
        elif action == "start_monitor":
            await self.monitoring_service.start()
            await query.message.reply_text("✅ Monitoring started!")
        elif action == "stop_monitor":
            await self.monitoring_service.stop()
            await query.message.reply_text("⏸️ Monitoring stopped!")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            return
        
        awaiting = context.user_data.get('awaiting')
        
        if awaiting == 'username_add':
            username = update.message.text.strip().lstrip('@')
            success = await self.data_manager.add_target(username)
            if success:
                await update.message.reply_text(f"✅ Added @{username} to monitoring list")
            else:
                await update.message.reply_text(f"❌ Could not add @{username} (already exists or limit reached)")
            context.user_data.pop('awaiting', None)
        
        elif awaiting == 'username_remove':
            username = update.message.text.strip().lstrip('@')
            success = await self.data_manager.remove_target(username)
            if success:
                await update.message.reply_text(f"✅ Removed @{username} from monitoring list")
            else:
                await update.message.reply_text(f"❌ @{username} not found in list")
            context.user_data.pop('awaiting', None)
    
    async def _show_status(self, chat_id: int):
        data = await self.data_manager.read()
        status = "🟢 Active" if data['settings']['monitoring_active'] else "🔴 Stopped"
        
        message = (
            f"📊 <b>Status Report</b>\n\n"
            f"Status: {status}\n"
            f"Monitored Users: {len(data['targets'])}/{config.MAX_MONITORED_USERS}\n"
            f"Poll Interval: {data['settings']['poll_seconds']}s\n"
            f"Page Pool Size: {config.PAGE_POOL_SIZE}\n"
        )
        
        await self.app.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
    
    async def _list_users(self, chat_id: int):
        data = await self.data_manager.read()
        
        if not data['targets']:
            await self.app.bot.send_message(chat_id=chat_id, text="📭 No usernames being monitored")
            return
        
        message = "📋 <b>Monitored Usernames:</b>\n\n"
        for i, username in enumerate(data['targets'], 1):
            message += f"{i}. @{username}\n"
        
        await self.app.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)
    
    def run(self):
        # PTB owns the event loop. Do not manually create/close loops.
        self.app = self._build_app()
        print("✅ Bot started successfully! Use /start in Telegram. Press Ctrl+C to stop...")
        self.app.run_polling(
            drop_pending_updates=True,
            allowed_updates=None,
            timeout=30,
            poll_interval=1.0,
        )
    
    async def cleanup(self):
        print("Cleaning up resources...")

        # Stop monitoring first so it won't use pages/context while we close them
        try:
            if self.monitoring_service:
                await self.monitoring_service.stop()
        except Exception as e:
            print(f"⚠️  Error stopping monitoring: {e}")

        # Close pages next
        try:
            if self.page_pool:
                await self.page_pool.cleanup()
        except Exception as e:
            print(f"⚠️  Error cleaning page pool: {e}")

        # Close context/browser defensively (they might already be closed)
        try:
            if self.context and hasattr(self.context, "is_closed") and not self.context.is_closed():
                await self.context.close()
        except Exception as e:
            # Ignore "Target page, context or browser has been closed"
            print(f"⚠️  Context already closed: {e}")

        try:
            if self.browser and hasattr(self.browser, "is_connected") and self.browser.is_connected():
                await self.browser.close()
        except Exception as e:
            print(f"⚠️  Browser already closed: {e}")

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            print(f"⚠️  Error stopping playwright: {e}")

        self.page_pool = None
        self.context = None
        self.browser = None
        self.playwright = None

        print("✅ Cleanup complete.")


if __name__ == "__main__":
    bot = TelegramBot()
    bot.run()
