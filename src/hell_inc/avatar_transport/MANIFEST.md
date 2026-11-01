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
- Component explicit avatar-ID start skips local image read; async on_created commits ownership before WSS; private emergency owned-ID receipt. Recording is explicitly disabled by default, including automatic recording retrieval. Credential strings remain opaque/header-safe; redirects disabled.
- Original motion/idle scheduler logic unchanged. Idle is not enabled or bound to old assets. Game wrapper delegates formal S / 1.1-second GE / 1.5-second Q path, with original successful Type9 gap and independent PCM writer locks.
- TTS keeps exact supplied text/instruction, explicit runtime voice required (no historical voice fallback), word timestamps always enabled by real adapter, exact PCM hashes retained. Audio download has no Authorization header. Private file writes; ffmpeg writes PCM to stdout then private file rather than public-mode output. Safe status/content-type evidence retained before SSE parsing; no signed URLs or raw secrets recorded.
- Timed planner validates but preserves original text/instruction whitespace. Enumeration, legal schedule selection and compilation unchanged.
- Register every nonempty opaque secret for diagnostic redaction, including short credentials; no prefix/length-based redaction exemption.
- Private diagnostic/artifact file modes 0600, directories 0700; original sanitization retained. No process-global umask change.

Tests using fake HTTP/SSE/socket/clock are not real model/TTS/Vidu/RTC acceptance. All real requests require separate explicit runtime opt-in and bounded authorization.

## Patched file SHA-256

- `__init__.py`: `32603d54ed03c98a9e0b33717e8053556975f62b061a154d619a8106fb2dc893`
- `component_live.py`: `9649dd8c0f6f92c38508d0634cec5a38f850d7557eb73784e59abc8897b3cc74`
- `diagnostics.py`: `3ca15a53bba546c915d2b4b5ef9473a8026a6629c182d9b143b0a79553905ace`
- `idle_motion.py`: `c9c8ac9792b0211ab8c212b1fb920574be98e4a76dc7c5a740677a0ddae2e271`
- `motion_component.py`: `b0e38a45b493cdbe01751f19cf865fe420a92007ed8400b331ab2ed58668a601`
- `timed_action_planner.py`: `888a9176f866def88c1fcc1ade6dc7f15b1bdd5e82457028e26112f9f9259b99`
- `tts_live.py`: `5c8bef1f64d8c754e2da375568f2d56de807ca8d0c2c70954a064bad9a714635`
