"""
pocket_adapter.py - من SmartyJoe/pocketbot
يجلب أسعار OTC الحقيقية من Pocket Option WebSocket
"""
import os
import asyncio

class PocketAdapter:
    def __init__(self):
        self.ssid = os.getenv("POCKET_OPTION_SSID") or ""
        self.connected = False
        self.api = None

    async def connect(self):
        if not self.ssid:
            print("No POCKET_OPTION_SSID - OTC unavailable")
            return False
        try:
            from binary_options_tools import PocketOption
