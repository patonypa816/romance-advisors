# RTC receiver provenance

Receive-only SDK choreography and capture protection adapted narrowly from
`avatar-studio/studio/static/player.js` at commit
`2e076b66ebd7b1db25d8c20914713ab7f4983f77`, blob
`25d33854c469c0baa2a27301dc8fc63f2e8a8844`.
No legacy player lifecycle, OBS controller, or Studio UI is imported.

AliRTC Web SDK 7.1.9 remains an optional external vendor artifact (not bundled
in source). Its original pinned SHA-256 is
`8100338351ce622a1d25e26997247e55b09135f458b90e748995b357ce26e376`.
`tools/fetch-rtc-sdk.sh` downloads only that pinned SDK and verifies before install.
Runtime loads same-origin `/vendor/aliyun-rtc-sdk-7.1.9.js` with matching SRI;
there is no CDN fallback or auto-install. SDK licensing remains with its provider.

## Original receiver license

MIT License

Copyright (c) 2026 Gary (曹淦)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

