#!/usr/bin/env python3
"""
Blink Camera Motion Watcher
Monitors Blink cameras for motion, captures photos, detects objects with YOLO,
and sends alerts via Telegram.
"""

import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime

import aiohttp
from dotenv import load_dotenv
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.helpers import util
from ultralytics import YOLO

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

# Configuration
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BLINK_USERNAME = os.getenv("BLINK_USERNAME", "")
BLINK_PASSWORD = os.getenv("BLINK_PASSWORD", "")

SNAPSHOT_COOLDOWN = int(os.getenv("SNAPSHOT_COOLDOWN", 10))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))

SNAPSHOT_DIR = BASE_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = BASE_DIR / ".blink_session"
YOLO_MODEL_PATH = BASE_DIR / "yolov8n.pt"

# Global YOLO model (lazy loaded)
_model = None


def get_model():
    """Lazy-load the YOLO model."""
    global _model
    if _model is None:
        logger.info("🤖 Loading YOLO model...")
        _model = YOLO(str(YOLO_MODEL_PATH))
    return _model


def detect_objects(image_path):
    """Detect specific objects in an image using YOLO."""
    model = get_model()
    results = model(str(image_path), verbose=False)

    # Classes we care about (mapping COCO class IDs to names)
    target_classes = {
        0: "person",
        15: "cat",
        16: "dog",
        17: "horse",
        18: "sheep",
        19: "cow",
    }

    found = set()
    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])

            if conf >= 0.5 and cls_id in target_classes:
                found.add(target_classes[cls_id])

    return list(found)


async def send_telegram(session, photo_path, caption):
    """Send a photo with a caption to Telegram asynchronously."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", TELEGRAM_CHAT_ID)
        data.add_field("caption", caption)
        data.add_field("photo", open(photo_path, "rb"), filename=photo_path.name)

        async with session.post(url, data=data, timeout=30) as resp:
            if resp.status == 200:
                logger.info("✅ Telegram alert sent")
                return True
            else:
                text = await resp.text()
                logger.error(f"❌ Telegram error: {resp.status} - {text}")
                return False
    except Exception as e:
        logger.error(f"❌ Failed to send Telegram: {e}")
        return False


class Watcher:
    """Monitors Blink cameras and coordinates actions."""
    def __init__(self):
        self.blink = None
        self.session = None
        self.last_motion = {}

    async def init_blink(self):
        """Initialize Blink connection and handle authentication."""
        self.session = aiohttp.ClientSession()
        
        login_data = None
        if SESSION_FILE.exists():
            try:
                login_data = await util.json_load(str(SESSION_FILE))
                logger.info("✅ Loaded saved Blink session")
            except Exception as e:
                logger.warning(f"⚠️ Could not load session: {e}")

        # If no saved session, use username/password
        if not login_data:
            login_data = {
                "username": BLINK_USERNAME,
                "password": BLINK_PASSWORD,
            }

        auth = Auth(
            login_data=login_data,
            no_prompt=True,
            session=self.session,
        )

        self.blink = Blink(session=self.session)
        self.blink.auth = auth

        try:
            await self.blink.start()
        except BlinkTwoFARequiredError:
            logger.info("🔐 2FA required. Enter code:")
            code = sys.stdin.readline().strip()
            if not code:
                logger.error("❌ No 2FA code provided")
                return False

            ok = await self.blink.auth.complete_2fa_login(code)
            if not ok:
                logger.error("❌ 2FA verification failed")
                return False
            
            # Restart after successful 2FA
            await self.blink.start()

        # Save session for next time
        try:
            await self.blink.save(str(SESSION_FILE))
            logger.info("💾 Blink session saved")
        except Exception as e:
            logger.error(f"❌ Failed to save session: {e}")

        if not self.blink.cameras:
            logger.error("❌ No cameras found")
            return False

        logger.info(f"✅ Monitoring cameras: {list(self.blink.cameras.keys())}")
        return True

    async def loop(self):
        """Main monitoring loop."""
        logger.info("🚀 Starting motion watcher loop...")
        try:
            while True:
                await self.blink.refresh()

                for name, cam in self.blink.cameras.items():
                    if not cam.motion_detected:
                        continue

                    # Cooldown check
                    now = time.time()
                    if now - self.last_motion.get(name, 0) < SNAPSHOT_COOLDOWN:
                        continue

                    self.last_motion[name] = now
                    logger.info(f"🚨 Motion on {name}")

                    # Capture and download image
                    await cam.snap_picture()
                    await self.blink.refresh()

                    # Save snapshot using the library's built-in authenticated downloader
                    ts = int(now)
                    fname = SNAPSHOT_DIR / f"{name}_{ts}.jpg"
                    
                    try:
                        await cam.image_to_file(str(fname))
                        logger.info(f"💾 Snapshot saved: {fname.name}")
                    except Exception as e:
                        logger.error(f"❌ Failed to download image for {name}: {e}")
                        continue

                    # Detection
                    detected = detect_objects(fname)
                    caption = f"🚨 {name}"
                    if detected:
                        caption += f"\nDetected: {', '.join(detected)}"

                    # Alert
                    await send_telegram(self.session, fname, caption)

                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("🛑 Loop cancelled")
        except Exception as e:
            logger.error(f"❌ Error in loop: {e}", exc_info=True)
        finally:
            if self.session:
                await self.session.close()

    async def close(self):
        """Close resources."""
        if self.session:
            await self.session.close()


async def main():
    watcher = Watcher()
    if not await watcher.init_blink():
        await watcher.close()
        return

    try:
        await watcher.loop()
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
    finally:
        await watcher.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
