#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
xcodebuild -project Jarvis.xcodeproj -scheme Jarvis -configuration Debug \
  -destination 'platform=macOS' -derivedDataPath .build/DerivedData \
  -clonedSourcePackagesDirPath .build/SourcePackages \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build
mkdir -p output
if [ -d output/Jarvis.app ]; then
  mv output/Jarvis.app "output/Jarvis.previous.$(date +%s).app"
fi
ditto .build/DerivedData/Build/Products/Debug/Jarvis.app output/Jarvis.app
codesign --verify --deep --strict --verbose=2 output/Jarvis.app
