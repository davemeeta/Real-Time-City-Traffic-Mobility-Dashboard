"""
ws.py  —  WebSocket live feed

Connects to Kafka consumer internally and broadcasts every new sensor
reading to all connected browser clients in real time.

Frontend connects via:  ws://localhost:8000/ws/live
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

log = logging.getLogger(__name__)
router = APIRouter()

# All currently connected WebSocket clients
_clients: Set[WebSocket] = set()

# Asyncio queue — Kafka thread puts messages here, async loop broadcasts them
_queue: asyncio.Queue = asyncio.Queue(maxsize=500)


async def _broadcast_loop():
    """
    Runs as a background task.
    Drains the queue and sends each message to all connected clients.
    """
    global _clients      
    while True:
        message = await _queue.get()
        dead = set()
        for ws in list(_clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        _clients -= dead


def _kafka_reader():
    """
    Runs in a separate thread (Kafka client is synchronous).
    Reads from traffic.sensors topic and puts JSON onto the asyncio queue.
    """
    import threading
    loop = asyncio.new_event_loop()

    def _run():
        try:
            consumer = KafkaConsumer(
                "traffic.sensors",
                bootstrap_servers="localhost:9092",
                group_id="ws-broadcaster",
                auto_offset_reset="latest",
                value_deserializer=lambda b: b.decode("utf-8"),
            )
            for msg in consumer:
                try:
                    _queue.put_nowait(msg.value)
                except asyncio.QueueFull:
                    pass   # drop oldest if no clients connected
        except NoBrokersAvailable:
            log.warning("Kafka not available — WebSocket feed will be silent.")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# Start Kafka reader thread once at module load
_kafka_reader()


@router.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    log.info(f"WS client connected. Total: {len(_clients)}")

    # Start broadcast loop as background task on first connection
    if len(_clients) == 1:
        asyncio.create_task(_broadcast_loop())

    try:
        while True:
            # Keep connection alive — client can send pings
            await ws.receive_text()
    except WebSocketDisconnect:
        _clients.discard(ws)
        log.info(f"WS client disconnected. Total: {len(_clients)}")

