#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import websockets
import json
import urllib.request
import time
import logging
import os

logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(message)s")

CLIENTS = set()
LAST_DATA = ""
WEBSOCKET_BIND_HOST = "127.0.0.1"
WEBSOCKET_PORT = 8765

async def fetch_and_broadcast():
    global LAST_DATA
    while True:
        try:
            if CLIENTS:  # Nur abfragen, wenn auch jemand zuschaut (spart Ressourcen!)
                req = urllib.request.Request("http://127.0.0.1/get_live_json.php")
                with urllib.request.urlopen(req, timeout=2) as response:
                    new_data = response.read().decode('utf-8')

                    # Nur senden, wenn sich die Daten geändert haben (Reduziert Traffic)
                    if new_data != LAST_DATA:
                        LAST_DATA = new_data
                        # An alle verbundenen Clients senden
                        for ws in list(CLIENTS):
                            try: await ws.send(new_data)
                            except: pass
        except Exception as e:
            pass # PHP Server evtl. kurzzeitig nicht erreichbar

        await asyncio.sleep(1) # Taktung für flüssige Animationen

async def handler(websocket, *args, **kwargs):
    client_ip = websocket.remote_address[0] if websocket.remote_address else "Unbekannt"
    logging.info(f"Neuer Client verbunden: {client_ip} (Gesamt: {len(CLIENTS)+1})")
    CLIENTS.add(websocket)
    if LAST_DATA:
        try: await websocket.send(LAST_DATA) # Sofort den letzten Stand senden
        except: pass

    try: await websocket.wait_closed()
    finally:
        CLIENTS.remove(websocket)
        logging.info(f"Client getrennt: {client_ip} (Gesamt: {len(CLIENTS)})")

async def main():
    logging.info(
        "Starte E3DC WebSocket Server auf %s:%s...",
        WEBSOCKET_BIND_HOST,
        WEBSOCKET_PORT,
    )
    asyncio.create_task(fetch_and_broadcast())
    async with websockets.serve(handler, WEBSOCKET_BIND_HOST, WEBSOCKET_PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
