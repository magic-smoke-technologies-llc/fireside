#!/usr/bin/env python3
"""Make the generated Android project release-signable.

`npx cap sync` regenerates android/app/build.gradle, so signing is injected on
every build from android_src/ rather than edited in place. Everything comes from
env vars so no password ever lands in the repo:

  FS_KEYSTORE   absolute path to the .jks/.p12 upload key
  FS_KS_PASS    store password
  FS_KEY_ALIAS  key alias
  FS_KEY_PASS   key password (falls back to store password)
  FS_VERSION_CODE / FS_VERSION_NAME   optional, bumped per upload

Usage: patch_build_gradle.py <path to android/app/build.gradle>
"""
import os
import re
import sys

MARKER = "// --- fireside release signing (injected by android_src/patch_build_gradle.py) ---"

BLOCK = """
%s
if (System.getenv("FS_KEYSTORE")) {
    android {
        signingConfigs {
            release {
                storeFile file(System.getenv("FS_KEYSTORE"))
                storePassword System.getenv("FS_KS_PASS")
                keyAlias System.getenv("FS_KEY_ALIAS")
                keyPassword System.getenv("FS_KEY_PASS") ?: System.getenv("FS_KS_PASS")
            }
        }
        buildTypes {
            release {
                signingConfig signingConfigs.release
            }
        }
    }
} else {
    logger.lifecycle("FS_KEYSTORE not set: release build will be UNSIGNED")
}
""" % MARKER


def main() -> int:
    path = sys.argv[1]
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if MARKER in src:
        print("signing block already present, leaving file alone")
        return 0

    code = os.environ.get("FS_VERSION_CODE")
    name = os.environ.get("FS_VERSION_NAME")
    if code:
        src, n = re.subn(r"versionCode\s+\d+", "versionCode %s" % int(code), src, count=1)
        if not n:
            print("warning: no versionCode line found", file=sys.stderr)
    if name:
        src, n = re.subn(r'versionName\s+"[^"]*"', 'versionName "%s"' % name, src, count=1)
        if not n:
            print("warning: no versionName line found", file=sys.stderr)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src.rstrip() + "\n" + BLOCK)

    print("patched %s (versionCode=%s versionName=%s)" % (path, code or "kept", name or "kept"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
