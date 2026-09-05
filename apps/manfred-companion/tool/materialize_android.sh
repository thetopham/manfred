#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTINATION="${1:-$SOURCE_DIR/.materialized}"

if ! command -v flutter >/dev/null 2>&1; then
  printf 'flutter is required on PATH\n' >&2
  exit 1
fi
if [[ -e "$DESTINATION" ]]; then
  printf 'refusing to overwrite existing destination: %s\n' "$DESTINATION" >&2
  exit 1
fi

flutter create --platforms=android --org com.thetopham --project-name manfred_companion "$DESTINATION"
cp "$SOURCE_DIR/pubspec.yaml" "$DESTINATION/pubspec.yaml"
cp "$SOURCE_DIR/analysis_options.yaml" "$DESTINATION/analysis_options.yaml"
rm -rf "$DESTINATION/lib" "$DESTINATION/test"
cp -R "$SOURCE_DIR/lib" "$DESTINATION/lib"
cp -R "$SOURCE_DIR/test" "$DESTINATION/test"
cp "$SOURCE_DIR/platform/android/AndroidManifest.xml" "$DESTINATION/android/app/src/main/AndroidManifest.xml"
mkdir -p "$DESTINATION/android/app/src/main/res/xml"
cp "$SOURCE_DIR/platform/android/network_security_config.xml" \
  "$DESTINATION/android/app/src/main/res/xml/network_security_config.xml"
# Keep native EyeVue source in the checked-in overlay, not generated Flutter files.
if [[ -d "$SOURCE_DIR/platform/android/kotlin" ]]; then
  mkdir -p "$DESTINATION/android/app/src/main/kotlin/com/thetopham/manfred_companion"
  cp -R "$SOURCE_DIR/platform/android/kotlin/." \
    "$DESTINATION/android/app/src/main/kotlin/com/thetopham/manfred_companion/"
fi
if [[ -d "$SOURCE_DIR/platform/android/test" ]]; then
  mkdir -p "$DESTINATION/android/app/src/test/kotlin/com/thetopham/manfred_companion"
  cp -R "$SOURCE_DIR/platform/android/test/." \
    "$DESTINATION/android/app/src/test/kotlin/com/thetopham/manfred_companion/"
fi
python3 - "$DESTINATION/android/app/build.gradle.kts" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
marker = "minSdk = flutter.minSdkVersion"
if s.count(marker) != 1:
    raise SystemExit("Unexpected Flutter minSdk template; refusing to guess")
s = s.replace(marker, "minSdk = 29")
s += """
// EyeVue BLE, coroutine lifecycle, and sockets scoped to the glasses network.
dependencies {
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
}
"""
p.write_text(s)
PY
(
  cd "$DESTINATION"
  flutter pub get
)
printf 'materialized Flutter Android project: %s\n' "$DESTINATION"
