# M4 performance protocol contract — Phase 1 + Phase 2

Real media acceptance remains pending. Fixed script is fetched through the same freeze endpoint with `{source:{kind:"fixed_script",id:"m4-demo-v1"}}`; no extra route or hardcoded client text.

## Implemented offline API contract (same current authenticated, same-Origin `/api` guard)

All UUIDs canonical strings. Unknown fields rejected. `expires_at` explicit UTC RFC3339. New record 201, idempotent repeat 200, conflict 409, invalid 400, missing 404. All snapshots `{revision,server_time,...}` with monotonic world revision. Never return credentials, PCM paths, RTC tokens, raw provider exceptions.

1. `GET /api/performance-session` → `{revision,server_time,session:{session_id,mode,status,media_verified:false,avatar_aspect:null|"16:9"|"4:3"}|null}`. Modes `offline_fixture` / `real`; real unavailable by default. Status `active|paused|closed|unknown`. Aspect is a view preference, no image transformation.
2. `POST /api/performance-session` body `{command_id,session_id,mode:"offline_fixture",avatar_aspect:null}`. Creates explicit LOCAL session only. Reject if another unclosed session, including unknown, exists. Same command payload replays stored receipt. No paid session creation route in this slice.
3. `POST /api/performance-session/{session_id}/pause` or `/resume` or `/close`, body `{command_id}`. Pause closes send gate immediately and cancels unstarted segments under sender gate. Return `{session,stop_status:"unstarted_cancelled"|"in_flight"|"unknown",...snapshot}`; never say media fully stopped. Resume manual only, never revives cancelled/expired/unknown records. Close rejects unresolved sending/unknown rather than allowing automatic new session.
4. `POST /api/performance-sources/freeze` body `{source:{kind:"fixed_script"|"interview_result"|"competition_comment"|"competition_result",id,submission_id?}}` → `{source_ref:{kind,id,submission_id?,source_version,source_hash},segments:[{text,intent}],content_hash}`. Read-only; resolve current persisted accepted/settled data, not caller-supplied scores/results. `competition_comment` only after all accepted reviews, at DISPLAYING or later; no winner/score leak. Persist exact authoritative source snapshot at submission. Fixed script uses `source_ref:{kind:"fixed_script",id:"m4-demo-v1"}` and server-owned source content; independent of players/rounds.
5. `POST /api/performances` body `{performance_id,session_id,source_ref,segments:[{text,intent}],expires_at,payload_hash}`. intent enum `question|listen|comment|result|neutral`. Server recomputes hash; rejects mismatch. For game references regenerate/compare frozen source and text (never allow arbitrary replacement text to masquerade as game result). Fixed script validated against server-owned canonical script. Return `{performance,...snapshot}`.
6. `GET /api/performances?session_id=...` → `{performances:[...],...snapshot}` bounded latest 50; `GET /api/performances/{performance_id}` same exact persisted record. Known terminal records still queryable after expiry/session changes.
7. `POST /api/performances/{performance_id}/execute` body `{command_id}`. Explicit offline execution only (background asyncio task), responds with current durable record, no network in transaction. Repeated command or execution of running/terminal ID never starts second run. No retry endpoint in this slice.
8. `POST /api/performances/{performance_id}/cancel` body `{command_id}` → `{performance,cancel_outcome:"unstarted_cancelled"|"in_flight"|"already_terminal"|"unknown",...snapshot}`. Current sending segment may finish; all unstarted remainder cancelled. Never promise remote abort.

Frontend uses only these agreed response shapes. `performance` includes `{performance_id,session_id,source_ref,source_snapshot,payload_hash,expires_at,state,segments:[{index,text,intent,state,result}],cancel_requested,mode,evidence,result,error_code,created_at,updated_at}`. `source_snapshot` is current authenticated workbench-only; public output omitted from scope. `evidence` always includes `{execution_kind:"offline_fixture"|"real",media_verified:false,audience_heard:false}`. Fixture final result carries `fixture_receipt`, never provider receipt.

## Persistence, hashing, state

Additive explicit v4 migration, accept v1/v2/v3/v4; startup requires v4 but never migrates. Preserve all M1–M3 records. Tables:

- `performance_sessions`: session_id PK, world_id FK, mode, status, body JSONB (aspect, pause reason, evidence), created_at/updated_at. Partial unique world_id where status != closed. No RTC secrets.
- `performances`: performance_id UUID PK, world_id FK, session_id FK, payload_hash String64, immutable_payload JSONB, state String32, body JSONB (per-segment states/results, cancellation and execution evidence), created_at/updated_at. CHECK state enum.
- `performance_commands`: (world_id,command_id) PK, payload JSONB, response JSONB, created_at; reuse existing strict command hashing/receipt pattern rather than generic workflow engine.

Canonical SHA256 over JSON UTF8, sorted keys, compact separators, no NaN: `{session_id,source_ref,segments,expires_at}` after canonical UTC millisecond timestamp normalization (`YYYY-MM-DDTHH:mm:ss.sssZ`); >millisecond precision is rejected. Preserve exact text Unicode and whitespace; no hidden trimming/rewrite. Include source reference/hash and intent. Exclude timestamps generated by server, mode/status, receipt, aspect layout. Same performance_id + same hash returns original persisted record BEFORE current expiry/source/session validation; same ID different hash 409 even after cancellation/unknown. This allows safe reconnection queries/repeats without accepting new work.

State: `accepted → preparing → ready → sending → send_completed`; pre-send failure `failed`; pre-send cancellation/expiry `cancelled|expired`; any uncertain post-attempt outcome `unknown`. Each segment follows same state pattern. A multi-segment performance stays sending after first segment starts; cancellation may leave completed prefix and cancelled suffix. Aggregate final `cancelled` with segment receipts and `result.partial=true`; never erase sent evidence. Any uncertain segment makes aggregate unknown. Check cancel/session/expiry before EVERY segment, not only first.

Persist `sending` and segment sending in short transaction BEFORE any provider write. Release DB transaction before awaiting adapter. One local sender mutex, same mutex for cancel/start decision; don't hold mutex for whole audio send or cancellation will block. Single worker claims one sending job plus at most one prepared next segment; reject excess accepted pending jobs with 409 queue_full (one pending performance in addition to current sufficient). DB busy/exception closes gate. A failed final DB commit after side effects is unknown, not failed/retryable.

Startup: all previous sending → unknown, preparing/ready/accepted retained but session paused and no automatic execution; offline in-flight tasks not silently completed. Real paid preparation interrupted is marked unknown preparation evidence, never automatically charged again. Manual resume permits only safe not-yet-started work; unknown not executable. Existing active game singleton lock remains required; no lease/fencing, distributed worker or automatic takeover.

## Source construction and boundaries

`interviews.py` stores body.evaluation, body.result, task IDs/input_hash/policy_hash. `competitions.py` stores body.submissions[*].evaluation.comment and task references, settled body.result. Use those actual sources; no second scoring or settlement call. Freeze deterministic brief result speech from authoritative decision/awards, preserve source snapshot plus IDs/version/hash. Competition existing comment can be 160 chars while avatar dynamic speech limit is 120: deterministically split exact text into ≤120-codepoint chunks, preferably at punctuation, preserving concatenation exactly; reject only if format cannot be represented safely. Backend policy should state maximum total/chunk count, e.g. 8 chunks / 960 codepoints. Do not silently truncate. Fixture asks no models and generates no real PCM.

Performance module must import no RecordService asset mutator and must not call settle/evaluation. Media status never opens voting, confirms presentation or changes game assets. Game pause stops future game performance submissions and requests performance pause; if confirmation unavailable show pending, not fully stopped. Add linkage only without changing current M2/M3 legal phase transitions.


## Phase 1 implementation boundaries (superseded for explicit real mode below)

No real session creation, provider clients, TTS, Vidu, RTC or paid preparation is wired. `offline_fixture` performs deterministic protocol calls without audio. Session aspect is layout metadata only; final broadcast context is 9:16 and avatar subregion is optionally landscape 16:9 / 4:3, unset by default.

Service: `PerformanceService(engine,world_id,adapter=None)`. Injectable adapters implement async `prepare(segment,cancellation)` and `send(prepared,session,still_valid)`; callbacks are synchronous booleans. The durable sending commit precedes adapter send. Known terminal IDs remain queryable. Pause/cancel can cancel only unstarted material, and in-flight completion is retained. Shutdown attempts unknown persistence; if the database is unavailable, sending rows are made unknown at next recovery. Manual execute commands are not auto-replayed after restart.

Game pause routes include `performance_pause` receipt after game pause commits. If its DB operation fails, HTTP returns unavailable rather than claiming media stopped; game pause remains durable. Game resume does not auto-resume the performance session.


## Phase 2: explicit real runtime, default OFF

Code supports an owned `RealMediaRuntime` and `RealPerformanceAdapter`; normal launch remains offline. This is mocked integration, **not real media acceptance**. No live provider or RTC requests were made during this implementation. No schema change beyond v4 is required: lifecycle facts reside in the session JSON body and per-segment preparation evidence.

### Stable API additions

All existing auth/origin/strict-field guards apply. `GET /api/performance-session` always adds top-level `media`, even with `session:null`; real sessions mirror it as `session.media`. Fields:

```
{state, owned_live_id, creation_attempted, cloud_end_confirmed,
 receiver:{claimed,healthy,joined,remote_video,audio_playback,autoplay_blocked,evidence_source:"client_reported"},
 operator_confirmed,real_enabled,paid_enabled}
```

States: `not_started|creating|connecting|waiting_receiver|ready|stopping|ended|unknown`. `media_verified` and `audience_heard` remain false. Receiver state is client-reported, operator confirmation is a separate manual observation—not audience proof.

- POST session with `mode:"real"` requires real opt-in and creates only a **local paused** row, never a cloud scene.
- POST `.../{id}/media/start` `{command_id,confirm_paid:true}` requires both opt-ins and validated configuration. It commits creation-attempted/creating and command receipt before one create call. Returns **202**, including command replay. A different command never creates again after any attempted/unknown outcome.
- POST `.../{id}/media/stop` `{command_id}` closes future preparation/send, cancels unstarted work, and performs original owned-component stop plus bounded end readback. **202**. This remains permitted after paid opt-in revocation/receiver loss. Pause alone does not end the cloud session.
- POST `.../{id}/media/reconcile` `{command_id}` performs provider **GET only**, using the persisted owned ID. **202**. Missing ID after ambiguous create reports `reconciliation_required:"provider_console"`; no arbitrary ID adoption or second create.
- POST `.../{id}/receiver/claim` `{receiver_id}` → **200** `{revision,server_time,receiver_id,receiver_token,epoch,rtc}`. `rtc` contains original provider join grant (`provider/app_id/channel_id/user_id/token/token_expire_at` when supplied), pinned local `sdk_url` and `sdk_integrity`. Only this authenticated no-store response carries tokens; never command receipts, DB, normal snapshots or browser storage. Same receiver refresh rotates the token and pauses; another receiver is rejected even after timeout.
- POST `.../{id}/receiver/heartbeat` `{receiver_id,receiver_token,joined,remote_video,audio_playback,autoplay_blocked}` → **200** session snapshot. All four flags strictly Boolean. Monotonic TTL is 10 seconds. Expiry/unhealthy state latches pause and invalidates operator confirmation; a late healthy heartbeat cannot resume automatically.
- POST `.../{id}/media/confirm-ready` `{command_id,receiver_id,confirm_audio_video:true}` → **200**. Requires current healthy claim; existing `/resume` remains separately required and also requires original component ready, durable ownership, current epoch and no unresolved performance.
- Real `/close` requires confirmed provider end if creation was attempted, and no in-flight worker. A never-started local session can close without inventing end evidence. After confirmed end, unknown performance records remain unknown/non-replayable but do not block closing the ended session. Historical close-command replay never clears a later runtime.

Late callbacks cannot reopen closed sessions or downgrade positive end readback. Runtime monitors capture their original component/epoch and are cancelled before releasing ownership.

### Preparation, sending and restart

Real adapter receives explicit `{performance_id,session_id,segment_index,payload_hash}`. Before every chargeable stage it durably records `preparation.attempted/phase/request_id` plus bounded history. Stage 1 output may contain **only instruction/actions/recovery**; text is composed from the authoritative frozen input. TTS is called exactly once with original whitespace, explicit configured voice and timestamps. Exact PCM hash/alignment is verified before the original constrained schedule-ID planner. No-action plans skip stage 2. No automatic provider retry or third fallback exists.

Per-segment preparation histories retain returned usage/PCM identity even when cancelled; no RTC tokens, private paths or raw provider exceptions are persisted. Restart during real preparation is unknown, never newly charged automatically. Real success uses `result.transport_receipt`, not `fixture_receipt`; it means local transport sent only. The original scheduler owns speech/socket locks and Type9/PCM sequencing; no direct Type9 shortcuts or old asset-bound idle resources are installed.

### Configuration and secure transport

Defaults: `HELL_INC_REAL_MEDIA_ENABLED=0`, `HELL_INC_ALLOW_PAID_MEDIA=0`. Both must be exactly `1` for create/prepare/send. Cleanup/reconciliation of already owned IDs does not depend on those flags.

Credentials use existing deployment variables `VIDU_API_KEY`, `DASHSCOPE_API_KEY`, `BAILIAN_WORKSPACE_ID`. Optional overrides are `HELL_INC_VIDU_KEY`, `HELL_INC_BAILIAN_KEY`, `HELL_INC_BAILIAN_WORKSPACE`: if present—even empty—they take precedence; explicit empty values fail, never fall back. Opaque nonempty header-safe keys are preserved without prefix assumptions, placeholder expansion, local CSV fallback or logging.

Required business configuration: `HELL_INC_AVATAR_ID`, `HELL_INC_PLANNER_MODEL`, `HELL_INC_TTS_VOICE`, `HELL_INC_VOICE_PERSONA`. There is no historical avatar/voice/persona default and no account asset ID in Git. `HELL_INC_MEDIA_DIR` defaults to `/tmp/hell-inc-media`, must be absolute, and should remain outside the checkout. Directories/files use 0700/0600. `HELL_INC_AUDIO_HOSTS` is an explicit comma-separated download-host allowlist, with HTTPS only, no credentials/userinfo/private IP/redirect. `HTTPS_PROXY` is explicitly injected into both HTTPS and WSS; ambient netrc/default auth is forbidden. Bailian uses Bearer, Vidu Token, and downloads get no Authorization. TLS verification remains on. Signed query strings are scrubbed.

The pinned local RTC SDK is `/vendor/aliyun-rtc-sdk-7.1.9.js`, SHA-256 `8100338351ce622a1d25e26997247e55b09135f458b90e748995b357ce26e376`. Both the serving route and production create preflight verify it. `HELL_INC_RTC_CONNECT_ORIGINS` defaults empty; only these pinned-source production candidates are accepted: `wss://rs.rtn.aliyuncs.com`, `https://rs.rtn.aliyuncs.com`, `https://gw.rtn.aliyuncs.com`. Real creation is unavailable without explicit origins and the SDK. These candidates were read from that SDK, **not verified by actual RTC connectivity**; no wildcard/preproduction/telemetry fallback is enabled. Browser CSP stays self-only by default. Any additional real endpoint must be independently reviewed before extension.

`MediaHTTP` uses a narrow aiohttp session override to force redirects off at `_request`, including the WebSocket HTTP Upgrade (aiohttp 3.14.4 `_ws_connect` otherwise uses default request redirects). An injected ordinary aiohttp session without that redirect policy is rejected for WSS. Fake sessions exist only for tests.

### Remaining acceptance

Provider credentials, actual model/TTS/Vidu responses, new avatar resources, RTC audio/video/autoplay, dynamic endpoint completeness, stop/end readback, billing and audience observations remain untested. Mocked provider/scheduler/browser tests cannot close those items. Any actual paid exercise requires separately bounded authorization.
