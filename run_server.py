import os
import threading
import webbrowser

from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

import app


def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def main() -> None:
    app.load_cache_from_disk()

    if env_bool("AI_MONITOR_PROXY_FIX"):
        app.app.wsgi_app = ProxyFix(app.app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    if env_bool("AI_MONITOR_ENABLE_SCHEDULER"):
        app.start_weekly_refresh_thread()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "8"))

    if env_bool("AI_MONITOR_OPEN_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open_new_tab(f"http://127.0.0.1:{port}")).start()

    serve(app.app, host=host, port=port, threads=threads, channel_timeout=120)


if __name__ == "__main__":
    main()
