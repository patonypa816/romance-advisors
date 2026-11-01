# Selective original transport snapshot

Source: `patonypa816/hell-inc-game`, pinned commit `2e076b66ebd7b1db25d8c20914713ab7f4983f77`, paths `avatar-studio/studio/<file>`. Read through GitHub connector; no provider calls. Original MIT license retained in LICENSE and package docstring. These are reviewed adapted copies, NOT claimed byte-identical files.

| Original file | Original Git blob SHA |
| --- | --- |
+| `component_live.py` | `5233b000ff5a6e743d0087bf4cda55c9e5ccb318` |
| `motion_component.py` | `aaf96c8eed136b1ee4a074bea7cdbe951fe1fec3` |
| `idle_motion.py` | `e5eb327d4592d7a637733c3038335ba8cc0236e2` |
| `tts_live.py` | `940f3ae0bda799f1d96f3c98e13aa34303189760` |
| `diagnostics.py` | `9370f67df7cb7c0ed1791c12cb02cdc0b0e90c1a` |
| `timed_action_planner.py` | `1584abfcc6e4df7b9d8c00d8d7ac9495334b2780` |

Narrow patches:
- Package-relative imports only; no sys.path mutation or credential-file fallback.
- Stop-only restart binding (`bind_owned_for_stop`) accepts only a fresh idle component and a validated already-owned live ID; it latches stop and reuses original `_finish`/Type1+Type5/close/GET. It cannot create, join RTC or send speech.
- Component explicit avatar-ID start skips local image read; async on_created commits ownership before WSS; private emergency owned-ID receipt. Cloud recording request defaults to true, matching the pinned original create body; runtime explicitly supplies a validated configuration (HELL_INC_MEDIA_RECORDING_ENABLED=0 opts out). Automatic recording retrieval is independently disabled by default. Public recording_requested is request configuration, not provider confirmation or saved-video evidence. Credential strings remain opaque/header-safe; redirects disabled.
- Original motion/idle scheduler logic unchanged. Idle is not enabled or bound to old assets. Game wrapper delegates formal S / 1.1-second GE / 1.5-second Q path, with original successful Type9 gap and independent PCM writer locks.
- TTS keeps exact supplied text/instruction, explicit runtime voice required (no historical voice fallback), word timestamps always enabled by real adapter, exact PCM hashes retained. Audio download has no Authorization header. Private file writes; ffmpeg writes PCM to stdout then private file rather than public-mode output. Safe status/content-type evidence retained before SSE parsing; no signed URLs or raw secrets recorded.
- Timed planner validates but preserves original text/instruction whitespace. Enumeration, legal schedule selection and compilation unchanged.
- Register every nonempty opaque secret for diagnostic redaction, including short credentials; no prefix/length-based redaction exemption.
- Private diagnostic/artifact file modes 0600, directories 0700; original sanitization retained. No process-global umask change.

Tests using fake HTTP/SSE/socket/clock are not real model/TTS/Vidu/RTC acceptance. All real requests require separate explicit runtime opt-in and bounded authorization.

## 2026-11-01 inline SSE audio compatibility patch

Official [Qwen-Audio-TTS HTTP response contract](https://help.aliyun.com/en/model-studio/qwen-audio-tts-http-api), read 2026-11-01: streaming `output.audio.data` contains base64 audio; sentence-synthesis chunks must be consumed in order and appended to one file. `output.finish_reason="stop"` marks normal completion. The documentation does not require a separate `[DONE]` sentinel.

Pinned source previously hashed decoded chunks but discarded them and unconditionally downloaded `audio.url`. The patch writes those exact chunks once to a private bounded `.part` file, validates complete SSE event boundaries and final `stop`, then verifies exact finite RIFF/WAVE chunk sizes (one PCM s16le/24 kHz/mono data payload). Only after validation is it promoted to the source WAV. Actual ffprobe/ffmpeg still run; converted PCM must additionally hash-identically match the WAV's original data payload. The timestamp provenance hash is bound to that same unmodified stream WAV and PCM. No header synthesis, resampling, silence padding, truncation, replay or second TTS call is introduced.

Default `audio_source="inline"` requires timestamp streaming; it does not contact the audio URL, even if supplied. Missing inline data, invalid base64, missing final stop, disconnected event, truncated/unknown-length RIFF or incompatible format fail explicitly, with private bounded partial evidence. There is no automatic URL fallback. Explicit `audio_source="download"` retains the existing headerless, redirect-disabled, approved-host-only HTTP wrapper path; its SSRF/proxy policy is unchanged. Optional `[DONE]` is accepted only after stop, never as a substitute for it. Stream request/usage evidence is retained on parse failures when available.

Fake SSE→real ffprobe/ffmpeg→original scheduler tests verify exact bytes and no audio-host GET. These establish mocked implementation behavior, not provider WAV layout or real audio acceptance; unsupported actual containers must fail and be reviewed, never guessed/repaired.

## Patched file SHA-256

- `__init__.py`: `32603d54ed03c98a9e0b33717e8053556975f62b061a154d619a8106fb2dc893`
- `component_live.py`: `5dccc5ad41aec1d82beddb0238dad2939d2a69ab82a3c9f097d2881c51a0226e`
- `diagnostics.py`: `3ca15a53bba546c915d2b4b5ef9473a8026a6629c182d9b143b0a79553905ace`
- `idle_motion.py`: `c9c8ac9792b0211ab8c212b1fb920574be98e4a76dc7c5a740677a0ddae2e271`
- `motion_component.py`: `b0e38a45b493cdbe01751f19cf865fe420a92007ed8400b331ab2ed58668a601`
- `timed_action_planner.py`: `888a9176f866def88c1fcc1ade6dc7f15b1bdd5e82457028e26112f9f9259b99`
- `tts_live.py`: `255860124ef40b41e6d65633cf56f66df5b982b0039e36d7400a627fef2b6c24`
