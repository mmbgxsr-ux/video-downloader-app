import os
import threading
import traceback
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.label import Label

ON_ANDROID = "ANDROID_ARGUMENT" in os.environ
STATE = {"error": None}  # set by any step that fails, shown on screen


def fail(prefix):
    STATE["error"] = prefix + "\n" + traceback.format_exc()


try:
    from video_downloader import H, PORT
except Exception:
    H, PORT = None, 8765
    fail("Cannot import video_downloader")

ANDROID = False
if ON_ANDROID:
    try:
        from android.runnables import run_on_ui_thread
        from jnius import autoclass
        ANDROID = True
    except Exception:
        fail("Cannot import android/jnius")


def start_server():
    # Bind before the WebView loads so the port is already listening.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if ANDROID:
    @run_on_ui_thread
    def open_webview(url):
        # Runs on the Android UI thread; errors are caught so they can be shown.
        try:
            WebView = autoclass("android.webkit.WebView")
            WebViewClient = autoclass("android.webkit.WebViewClient")
            Color = autoclass("android.graphics.Color")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            wv = WebView(activity)
            settings = wv.getSettings()
            settings.setJavaScriptEnabled(True)
            settings.setDomStorageEnabled(True)
            wv.setBackgroundColor(Color.parseColor("#0f172a"))
            wv.setWebViewClient(WebViewClient())
            activity.setContentView(wv)
            wv.loadUrl(url)
        except Exception:
            fail("Cannot open WebView")


class DownloaderApp(App):
    def build(self):
        # English text on purpose: Kivy's default font has no Arabic glyphs.
        self.label = Label(text="Starting...", halign="center", valign="middle")
        self.label.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - 40, s[1])))
        self.server = None
        if STATE["error"] is None:
            try:
                self.server = start_server()
            except Exception:
                fail("Cannot start local server on port %d" % PORT)
        Clock.schedule_interval(self.refresh_error, 1)
        return self.label

    def refresh_error(self, dt):
        # Visible only while the Kivy view is on screen (i.e. WebView not shown).
        if STATE["error"]:
            self.label.text = "Error:\n" + STATE["error"][-1500:]

    def on_start(self):
        Clock.schedule_once(self.start_ui, 0.3)

    def start_ui(self, dt):
        if STATE["error"] or self.server is None:
            return
        url = "http://127.0.0.1:%d/" % PORT
        try:
            urllib.request.urlopen(url, timeout=3).read(1)
        except Exception:
            fail("Local server did not answer at " + url)
            return
        if ANDROID:
            open_webview(url)
        else:
            self.label.text = "Open " + url
            webbrowser.open(url)

    def on_pause(self):
        return True  # keep the app (and the download server) alive in background

    def on_stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


if __name__ == "__main__":
    DownloaderApp().run()
