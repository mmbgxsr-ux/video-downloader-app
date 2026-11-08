[app]
title = تحميل الفيديوهات
package.name = videodl
package.domain = org.videodl
source.dir = .
source.include_exts = py,html,js,css,png,jpg,json
version = 1.1
requirements = python3,kivy==2.3.0,pyjnius,android,yt-dlp,certifi,openssl
orientation = portrait
fullscreen = 0
p4a.branch = v2024.01.21
android.permissions = INTERNET,FOREGROUND_SERVICE,POST_NOTIFICATIONS,WRITE_EXTERNAL_STORAGE
services = Downloader:service.py:foreground
android.manifest.intent_filters = intent_filters.xml
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.extra_manifest_application_arguments = cleartext.txt
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so

[buildozer]
log_level = 2
warn_on_root = 1
