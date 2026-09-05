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
(
  cd "$DESTINATION"
  flutter pub get
)
printf 'materialized Flutter Android project: %s\n' "$DESTINATION"
