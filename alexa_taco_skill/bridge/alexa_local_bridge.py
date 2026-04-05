#!/usr/bin/env python3
"""
Local Alexa Skill Bridge Server
Exposes /alexa/query endpoint that wraps alexa_remote_control.sh
Provides local fallback when AWS Lambda is down
"""

import os
import sys
import asyncio
import logging
import subprocess
import json
import re
from aiohttp import web
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
ALEXA_SCRIPT = "/home/abdaltm86/.openclaw/workspace/alexa-remote-control/alexa_remote_control.sh"
PORT = 5001  # Different from taco_api_server on 5000
DEVICE = "Abdallah's Echo Show 5 3rd Gen"

routes = web.RouteTableDef()


def extract_intent_name(request_body):
    """Extract intent name from Alexa JSON request."""
    try:
        return request_body.get("request", {}).get("intent", {}).get("name", "")
    except:
        return ""


def extract_slots(request_body):
    """Extract slots from Alexa JSON request."""
    try:
        return request_body.get("request", {}).get("intent", {}).get("slots", {})
    except:
        return {}


def get_slot_value(slots, slot_name):
    """Get slot value from slots dict."""
    try:
        return slots.get(slot_name, {}).get("value", "")
    except:
        return ""


@routes.post("/alexa/query")
async def alexa_query(request):
    """Main endpoint that handles Alexa requests."""
    try:
        body = await request.json()
        intent_name = extract_intent_name(body)
        slots = extract_slots(body)

        logger.info(f"Alexa request: intent={intent_name}")

        # Handle different intents
        if intent_name == "TacoAnnounce":
            # Extract announcement text
            message = get_slot_value(slots, "Message") or get_slot_value(slots, "message")
            if message:
                logger.info(f"Announcement: {message}")
                result = subprocess.run(
                    ["bash", ALEXA_SCRIPT, "-d", DEVICE, "-e", f"speak:{message}"],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    return web.json_response({
                        "version": "1.0",
                        "response": {
                            "shouldEndSession": True,
                            "outputSpeech": {
                                "type": "PlainText",
                                "text": f"Announced: {message}"
                            }
                        }
                    })
                else:
                    return web.json_response({
                        "version": "1.0",
                        "response": {
                            "shouldEndSession": True,
                            "outputSpeech": {
                                "type": "PlainText",
                                "text": "Failed to announce"
                            }
                        }
                    })

        elif intent_name == "TacoStatus":
            return web.json_response({
                "version": "1.0",
                "response": {
                    "shouldEndSession": True,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": "Taco is running. Blink Cam is active with face recognition."
                    }
                }
            })

        elif intent_name == "TacoReport":
            return web.json_response({
                "version": "1.0",
                "response": {
                    "shouldEndSession": True,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": "Trading report not available on local bridge. Check your dashboard."
                    }
                }
            })

        elif intent_name == "AMAZON.HelpIntent":
            return web.json_response({
                "version": "1.0",
                "response": {
                    "shouldEndSession": False,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": "Taco Local Bridge is ready. Say 'announce' followed by your message to send it to your Alexa device."
                    },
                    "reprompt": {
                        "outputSpeech": {
                            "type": "PlainText",
                            "text": "What would you like to announce?"
                        }
                    }
                }
            })

        elif intent_name == "AMAZON.StopIntent" or intent_name == "AMAZON.CancelIntent":
            return web.json_response({
                "version": "1.0",
                "response": {
                    "shouldEndSession": True,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": "Goodbye from Taco!"
                    }
                }
            })

        else:
            return web.json_response({
                "version": "1.0",
                "response": {
                    "shouldEndSession": True,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": f"Taco local bridge received: {intent_name or 'unknown request'}"
                    }
                }
            })

    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return web.json_response({
            "version": "1.0",
            "response": {
                "shouldEndSession": True,
                "outputSpeech": {
                    "type": "PlainText",
                    "text": "An error occurred processing your request."
                }
            }
        }, status=500)


@routes.get("/health")
async def health(request):
    """Health check endpoint."""
    return web.json_response({"status": "ok", "service": "alexa-local-bridge"})


@routes.get("/")
async def index(request):
    """Index page."""
    return web.json_response({
        "service": "Alexa Local Bridge",
        "version": "1.0",
        "endpoints": {
            "alexa_query": "POST /alexa/query",
            "health": "GET /health"
        }
    })


async def init_app():
    """Initialize the aiohttp app."""
    app = web.Application()
    app.add_routes(routes)
    return app


def main():
    """Start the server."""
    logger.info(f"Starting Alexa Local Bridge on port {PORT}")
    logger.info(f"Using Alexa script: {ALEXA_SCRIPT}")
    logger.info(f"Device: {DEVICE}")

    app = init_app()
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
