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


def serve():
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


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
        threading.Thread(target=serve, daemon=True).start()
        return Label(text="...")

    def on_start(self):
        if ANDROID:
            request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                                 Permission.READ_EXTERNAL_STORAGE])
            open_webview()


if __name__ == "__main__":
    DownloaderApp().run()
