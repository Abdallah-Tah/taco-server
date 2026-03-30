#!/usr/bin/env python3
"""
Blink Camera Motion Watcher with Face Recognition
Monitors Blink cameras for motion, captures photos, detects faces/animals, sends Telegram alerts
"""

import os
import sys
import time
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
SCRIPT_DIR = Path(__file__).parent
ENV_FILE = SCRIPT_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Configuration from environment
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7520899464")
BLINK_USERNAME = os.environ.get("BLINK_USERNAME", "")
BLINK_PASSWORD = os.environ.get("BLINK_PASSWORD", "")
SNAPSHOT_COOLDOWN = int(os.environ.get("SNAPSHOT_COOLDOWN", "30"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))

# Paths
SNAPSHOT_DIR = Path.home() / ".openclaw" / "workspace" / "camera" / "snapshots"
LOG_FILE = Path.home() / ".openclaw" / "workspace" / "camera" / "blink_motion.log"
KNOWN_FACES_DIR = SCRIPT_DIR / "known_faces"

SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
KNOWN_FACES_DIR.mkdir(parents=True, exist_ok=True)


def log(message):
    """Log message to file and print"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(log_entry + "\n")


def send_telegram_photo(photo_path, caption):
    """Send photo with caption to Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ Telegram credentials not configured")
        return False
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as photo:
            files = {"photo": photo}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            resp = requests.post(url, files=files, data=data, timeout=30)
            if resp.status_code == 200:
                log(f"✅ Telegram alert sent: {caption}")
                return True
            else:
                log(f"❌ Telegram API error: {resp.status_code} - {resp.text}")
                return False
    except Exception as e:
        log(f"❌ Telegram send failed: {e}")
        return False


def detect_faces_and_animals(image_path):
    """Detect faces and animals in image"""
    try:
        import cv2
        import numpy as np
        import face_recognition
        from ultralytics import YOLO
        
        # Load image
        image = face_recognition.load_image_file(str(image_path))
        cv_image = cv2.imread(str(image_path))
        
        results = {"faces": [], "animals": []}
        
        # Face detection
        try:
            face_locations = face_recognition.face_locations(image)
            face_encodings = face_recognition.face_encodings(image, face_locations)
            
            # Load known faces
            known_encodings = []
            known_names = []
            for known_file in KNOWN_FACES_DIR.glob("*.jpg"):
                try:
                    known_image = face_recognition.load_image_file(str(known_file))
                    known_encoding = face_recognition.face_encodings(known_image)[0]
                    known_encodings.append(known_encoding)
                    known_names.append(known_file.stem)
                except Exception:
                    pass
            
            for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
                name = "unknown"
                if known_encodings:
                    matches = face_recognition.compare_faces(known_encodings, face_encoding, tolerance=0.6)
                    face_distances = face_recognition.face_distance(known_encodings, face_encoding)
                    if len(face_distances) > 0:
                        best_match_idx = np.argmin(face_distances)
                        if matches[best_match_idx]:
                            name = known_names[best_match_idx]
                
                results["faces"].append({
                    "name": name,
                    "location": (top, right, bottom, left),
                    "confidence": 1.0 - (np.min(face_distances) if len(face_distances) > 0 else 1.0)
                })
        except Exception as e:
            log(f"⚠️ Face detection error: {e}")
        
        # Animal detection with YOLO
        try:
            model = YOLO("yolov8n.pt")
            cv_img = cv2.imread(str(image_path))
            yolo_results = model(cv_img, verbose=False)
            
            animal_classes = {
                14: "dog", 15: "cat", 16: "horse", 17: "sheep", 18: "cow",
                19: "elephant", 20: "bear", 21: "zebra", 22: "giraffe",
                0: "person"
            }
            
            for r in yolo_results:
                boxes = r.boxes
                for box in boxes:
                    cls = int(box.cls[0])
                    conf = float(box.conf[0])
                    if conf > 0.5 and cls in animal_classes:
                        results["animals"].append({
                            "class": animal_classes[cls],
                            "confidence": conf
                        })
        except Exception as e:
            log(f"⚠️ Animal detection error: {e}")
        
        return results
    except ImportError as e:
        log(f"⚠️ Detection libraries not available: {e}")
        return {"faces": [], "animals": []}
    except Exception as e:
        log(f"⚠️ Detection error: {e}")
        return {"faces": [], "animals": []}


class BlinkMotionWatcher:
    def __init__(self):
        self.blink = None
        self.cameras = {}
        self.last_snapshot = {}
        self.motion_count = 0
        
    async def init_blink(self):
        """Initialize Blink connection"""
        try:
            from blinkpy import blinkpy
            from blinkpy.auth import Auth
            from blinkpy.helpers.util import get_auth_entry
            
            log("📷 Initializing Blink connection...")
            
            if not BLINK_USERNAME or not BLINK_PASSWORD:
                log("❌ ERROR: BLINK_USERNAME and BLINK_PASSWORD not set in .env")
                return False
            
            self.blink = blinkpy.Blink()
            auth = Auth({
                "username": BLINK_USERNAME,
                "password": BLINK_PASSWORD
            })
            self.blink.auth = auth
            
            # Try to load saved session
            session_file = SCRIPT_DIR / ".blink_session"
            if session_file.exists():
                try:
                    self.blink.auth.load_auth(str(session_file))
                    await self.blink.start()
                    log("✅ Loaded saved Blink session")
                except Exception:
                    log("⚠️ Saved session invalid, re-authenticating...")
            
            # Fresh auth
            if not self.blink.cameras:
                log("🔐 Authenticating with Blink...")
                await self.blink.start()
                # Save session
                self.blink.auth.save_auth(str(session_file))
                log("✅ Blink session saved")
            
            self.cameras = self.blink.cameras
            if not self.cameras:
                log("❌ No Blink cameras found")
                return False
            
            log(f"✅ Connected to {len(self.cameras)} camera(s): {', '.join(self.cameras.keys())}")
            return True
            
        except Exception as e:
            log(f"❌ Blink init failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def check_motion(self):
        """Check all cameras for motion"""
        try:
            await self.blink.refresh()
            motion_detected = False
            
            for name, camera in self.cameras.items():
                try:
                    # Check motion status
                    motion_status = camera.motion_detected
                    
                    if motion_status:
                        motion_detected = True
                        self.motion_count += 1
                        
                        # Check cooldown
                        now = time.time()
                        last = self.last_snapshot.get(name, 0)
                        if now - last < SNAPSHOT_COOLDOWN:
                            log(f"📸 {name}: motion detected (cooldown active)")
                            continue
                        
                        self.last_snapshot[name] = now
                        
                        log(f"🚨 Motion detected on {name}! (#{self.motion_count})")
                        
                        # Capture image
                        await camera.snap_picture()
                        await self.blink.refresh()
                        
                        # Get image URL
                        image_url = camera.image_path
                        if not image_url:
                            log(f"⚠️ No image available for {name}")
                            continue
                        
                        # Download image
                        import requests
                        img_resp = requests.get(image_url, timeout=10)
                        if img_resp.status_code != 200:
                            log(f"⚠️ Failed to download image for {name}")
                            continue
                        
                        # Save snapshot
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        snapshot_path = SNAPSHOT_DIR / f"blink_{name}_{timestamp}.jpg"
                        with open(snapshot_path, "wb") as f:
                            f.write(img_resp.content)
                        log(f"📸 Snapshot saved: {snapshot_path.name}")
                        
                        # Analyze image
                        detection_results = detect_faces_and_animals(snapshot_path)
                        
                        # Build caption
                        caption_parts = [f"🚨 Blink Alert: {name} (#{self.motion_count})"]
                        
                        faces = detection_results.get("faces", [])
                        if faces:
                            face_names = [f["name"] for f in faces]
                            if "unknown" in face_names:
                                caption_parts.append("⚠️ Unknown person detected")
                            else:
                                caption_parts.append(f"👤 Known: {', '.join(set(face_names))}")
                        else:
                            caption_parts.append("👤 No faces detected")
                        
                        animals = detection_results.get("animals", [])
                        if animals:
                            animal_list = [f"{a['class']} ({a['confidence']:.0%})" for a in animals]
                            caption_parts.append(f"🐾 Detected: {', '.join(animal_list)}")
                        
                        caption = "\n".join(caption_parts)
                        
                        # Send to Telegram
                        send_telegram_photo(str(snapshot_path), caption)
                        
                except Exception as e:
                    log(f"⚠️ Error checking {name}: {e}")
            
            return motion_detected
            
        except Exception as e:
            log(f"❌ Motion check failed: {e}")
            return False
    
    async def run(self):
        """Main monitoring loop"""
        log("=" * 60)
        log("🔴 Blink Motion Watcher Started")
        log("=" * 60)
        log(f"Snapshot cooldown: {SNAPSHOT_COOLDOWN}s")
        log(f"Poll interval: {POLL_INTERVAL}s")
        log(f"Telegram chat: {TELEGRAM_CHAT_ID}")
        log("=" * 60)
        
        if not await self.init_blink():
            return 1
        
        last_status = time.time()
        
        try:
            while True:
                await self.check_motion()
                
                # Status every 5 minutes
                if time.time() - last_status > 300:
                    log(f"📊 Status: {self.motion_count} total motions detected")
                    last_status = time.time()
                
                await asyncio.sleep(POLL_INTERVAL)
                
        except KeyboardInterrupt:
            log("👋 Shutting down...")
        except Exception as e:
            log(f"❌ Fatal error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.blink:
                try:
                    await self.blink.stop()
                except Exception:
                    pass
        
        return 0


if __name__ == "__main__":
    watcher = BlinkMotionWatcher()
    sys.exit(asyncio.run(watcher.run()))
