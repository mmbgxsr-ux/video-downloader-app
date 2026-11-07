"""Android foreground service: runs the download server in its own process so
downloads keep going when the app is in the background or the screen is off.

It stops itself after a couple of minutes without any request and no running
download, so the notification does not stay forever."""
import os
import time
import traceback

import video_downloader as vd

IDLE_SECONDS = 120


def stop_service():
    try:
        from jnius import autoclass
        autoclass("org.kivy.android.PythonService").mService.stopSelf()
    except Exception:
        pass
    os._exit(0)


def main():
    try:
        vd.start_server()
    except Exception:
        # Most likely the port is already served (another service/app instance).
        traceback.print_exc()
        return
    while True:
        time.sleep(5)
        if not vd.is_busy() and time.time() - vd.LAST_ACTIVITY > IDLE_SECONDS:
            stop_service()


main()
