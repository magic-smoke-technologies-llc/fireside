#!/bin/sh
# Build the Play-uploadable Fireside .aab on bench01.
#
# Same pipeline as build_apk.sh up to the native pieces, then a signed release
# bundle instead of a debug APK. Requires the upload key, out of the repo:
#
#   export FS_KEYSTORE=/srv/bench/fieldstone/android-keys/fireside-upload.jks
#   export FS_KS_PASS=...           # vault: fireside-android-upload-keystore-password
#   export FS_KEY_ALIAS=fireside-upload
#   export FS_VERSION_CODE=2
#   export FS_VERSION_NAME=1.0.0
#
# Usage: ./build_aab.sh   (refuses to build unsigned)
set -e
[ -f /srv/bench/android_env.sh ] && . /srv/bench/android_env.sh
export PATH=/srv/bench/tools/node-v22.11.0-linux-x64/bin:$PATH
cd "$(dirname "$0")"

: "${FS_KEYSTORE:?FS_KEYSTORE must point at the upload keystore}"
: "${FS_KS_PASS:?FS_KS_PASS must be the store password}"
: "${FS_KEY_ALIAS:=fireside-upload}"
export FS_KEY_ALIAS
[ -e "$FS_KEYSTORE" ] || { echo "keystore not found: $FS_KEYSTORE" >&2; exit 1; }

npm install --no-audit --no-fund
[ -d android ] || npx cap add android
npx cap sync android

mkdir -p android/app/src/main/java/com/stolworthy/fireside
cp android_src/MainActivity.java android_src/MediaPlaybackService.java \
   android/app/src/main/java/com/stolworthy/fireside/
python3 android_src/patch_manifest.py android/app/src/main/AndroidManifest.xml
python3 android_src/patch_build_gradle.py android/app/build.gradle

[ -f android/local.properties ] || echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android
sh ./gradlew bundleRelease
OUT=app/build/outputs/bundle/release/app-release.aab
ls -l "$OUT"
echo "--- signature"
"$JAVA_HOME/bin/keytool" -printcert -jarfile "$OUT" | sed -n '1,12p'
