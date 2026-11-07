import threading
from http.server import ThreadingHTTPServer
from kivy.app import App
from kivy.uix.label import Label
from video_downloader import H, PORT

try:
    from android.runnables import run_on_ui_thread
    from android.permissions import request_permissions, Permission
    from jnius import autoclass
    ANDROID = True
except Exception:
    ANDROID = False


def start_server():
    # Bind synchronously so the port is already listening when the WebView
    # loads the page (starting the server inside the thread raced with it).
    server = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if ANDROID:
    @run_on_ui_thread
    def open_webview():
        WebView = autoclass("android.webkit.WebView")
        WebViewClient = autoclass("android.webkit.WebViewClient")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        wv = WebView(activity)
        wv.getSettings().setJavaScriptEnabled(True)
        wv.setWebViewClient(WebViewClient())
        activity.setContentView(wv)
        wv.loadUrl("http://127.0.0.1:%d" % PORT)


class DownloaderApp(App):
    def build(self):
        self.server = start_server()
        return Label(text="...")

    def on_stop(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()

    def on_start(self):
        if ANDROID:
            request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                                 Permission.READ_EXTERNAL_STORAGE])
            open_webview()


if __name__ == "__main__":
    DownloaderApp().run()
