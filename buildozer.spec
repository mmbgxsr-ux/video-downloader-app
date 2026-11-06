[app]
title = تحميل الفيديوهات
package.name = videodl
package.domain = org.videodl
source.dir = .
source.include_exts = py
version = 1.0
requirements = python3,kivy==2.3.0,pyjnius,android,yt-dlp,certifi,openssl
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.extra_manifest_application_arguments = android:usesCleartextTraffic="true"

[buildozer]
log_level = 2
warn_on_root = 1
