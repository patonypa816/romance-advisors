# M4 offline protocol contract

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


## Implementation boundaries

No real session creation, provider clients, TTS, Vidu, RTC or paid preparation is wired. `offline_fixture` performs deterministic protocol calls without audio. Session aspect is layout metadata only; final broadcast context is 9:16 and avatar subregion is optionally landscape 16:9 / 4:3, unset by default.

Service: `PerformanceService(engine,world_id,adapter=None)`. Injectable adapters implement async `prepare(segment,cancellation)` and `send(prepared,session,still_valid)`; callbacks are synchronous booleans. The durable sending commit precedes adapter send. Known terminal IDs remain queryable. Pause/cancel can cancel only unstarted material, and in-flight completion is retained. Shutdown attempts unknown persistence; if the database is unavailable, sending rows are made unknown at next recovery. Manual execute commands are not auto-replayed after restart.

Game pause routes include `performance_pause` receipt after game pause commits. If its DB operation fails, HTTP returns unavailable rather than claiming media stopped; game pause remains durable. Game resume does not auto-resume the performance session.
