"""Generate once per Shanghai calendar day while the local backend runs."""

import asyncio
from datetime import datetime
import logging
from uuid import NAMESPACE_URL, uuid5

from .menu_service import SHANGHAI

logger = logging.getLogger(__name__)


def generate_if_due(service, now):
    local = now.astimezone(SHANGHAI)
    if local.hour < 6:
        return None
    day = local.date().isoformat()
    key = uuid5(NAMESPACE_URL, f"household-menu:scheduled:{day}")
    return service.write_menu("generate", day, key, {})


async def run_scheduler(service):
    completed_day = None
    while True:
        now = datetime.now(SHANGHAI)
        delay = 15
        if now.date() != completed_day:
            try:
                result = await asyncio.to_thread(generate_if_due, service, now)
            except Exception:
                logger.warning("Scheduled menu generation failed; will retry in five minutes")
                delay = 300
            else:
                if result is not None:
                    completed_day = now.date()
        await asyncio.sleep(delay)
