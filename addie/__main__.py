"""Run with:  python -m addie"""
import threading
import webbrowser

import uvicorn

from . import config
from .api import create_app


def main():
    url = f"http://{config.HOST}:{config.PORT}/"
    print(f"Addie רץ בכתובת {url}  (הנתונים נשמרים ב-{config.DATA_DIR})")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
