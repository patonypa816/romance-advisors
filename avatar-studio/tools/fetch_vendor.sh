#!/bin/sh
# Verified AliRTC Web SDK only; no server, credentials, or cloud session.
set -eu
cd "$(dirname "$0")/.."
target=static/vendor/aliyun-rtc-sdk-7.1.9.js
checksum=8100338351ce622a1d25e26997247e55b09135f458b90e748995b357ce26e376
verify() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$checksum" "$1" | sha256sum -c - >/dev/null 2>&1
    else
        printf '%s  %s\n' "$checksum" "$1" | shasum -a 256 -c - >/dev/null 2>&1
    fi
}
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    echo "A SHA-256 verifier is required." >&2
    exit 1
fi
if [ -f "$target" ] && verify "$target"; then
    echo "RTC SDK 7.1.9 already verified."
    exit 0
fi
mkdir -p static/vendor
temporary=$(mktemp "$target.tmp.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
curl --proto '=https' --proto-redir '=https' -fsSL --connect-timeout 15 --max-time 120 \
    -o "$temporary" https://g.alicdn.com/apsara-media-box/imp-web-rtc/7.1.9/aliyun-rtc-sdk.js
if ! verify "$temporary"; then
    echo "RTC SDK checksum mismatch; existing target left unchanged." >&2
    exit 1
fi
mv "$temporary" "$target"
echo "RTC SDK 7.1.9 verified."
