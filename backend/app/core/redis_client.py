import json
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

SLOTS_CACHE_KEY = "parking:slots_map"
EVENTS_LOG_KEY = "parking:device_events"


async def get_cached_slots() -> Optional[List[dict[str, Any]]]:
    data = await redis_client.get(SLOTS_CACHE_KEY)
    return json.loads(data) if data else None


async def set_cached_slots(slots_data: List[dict[str, Any]], ttl: int = 30) -> None:
    await redis_client.set(SLOTS_CACHE_KEY, json.dumps(slots_data), ex=ttl)


async def invalidate_slots_cache() -> None:
    await redis_client.delete(SLOTS_CACHE_KEY)


async def log_device_event(device: str, event_type: str, message: str, is_error: bool = False) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "device": device,
        "event_type": event_type,
        "message": message,
        "is_error": is_error,
    }
    await redis_client.lpush(EVENTS_LOG_KEY, json.dumps(event, ensure_ascii=False))
    await redis_client.ltrim(EVENTS_LOG_KEY, 0, 49)  # Зберігаємо останні 50 подій


async def get_device_events(limit: int = 20) -> List[dict[str, Any]]:
    raw_events = await redis_client.lrange(EVENTS_LOG_KEY, 0, limit - 1)
    return [json.loads(item) for item in raw_events]