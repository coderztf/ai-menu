"""Run the server with one log file per Shanghai calendar day."""
from datetime import datetime
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn


class DailyLog(logging.Handler):
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def emit(self, record):
        try:
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            with (self.directory / (day + ".log")).open("a", encoding="utf-8") as output:
                output.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


if __name__ == "__main__":
    handler = DailyLog(os.environ.get("MENU_LOG_DIR", "/opt/ai-menu/logs"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    uvicorn.run("ai_menu.api:app", host=os.environ.get("MENU_HOST", "0.0.0.0"),
                port=int(os.environ.get("MENU_PORT", "8000")), log_config=None)
