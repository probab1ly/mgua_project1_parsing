import os
import threading
import webbrowser

from waitress import serve

import app


def main() -> None:
    app.load_cache_from_disk()
    if os.environ.get("AI_MONITOR_ENABLE_SCHEDULER", "1").lower() in {"1", "true", "yes"}:
        app.start_weekly_refresh_thread()
    if os.environ.get("AI_MONITOR_OPEN_BROWSER", "1").lower() in {"1", "true", "yes"}:
        threading.Timer(1.0, lambda: webbrowser.open_new_tab("http://127.0.0.1:5000")).start()
    serve(app.app, host="127.0.0.1", port=5000, threads=8, channel_timeout=120)


if __name__ == "__main__":
    main()
