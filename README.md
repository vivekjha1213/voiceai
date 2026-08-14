# TwoCare Clinic Voice AI Receptionist

A voice AI front-desk for a two-branch clinic (Gurugram + Noida), built on LiveKit
Agents with OpenAI Realtime as the speech-to-speech layer, a FastAPI backend with a
conflict-safe Postgres scheduling core, and a Cliniko PMS write-back integration.

**Status: scaffold, not a tested-live deployment.** Everything below was written and
structured to be correct and runnable, but I have not run it against your live
Cliniko sandbox, LiveKit Cloud project, or OpenAI Realtime endpoint — see
[Known limitations](#known-limitations) for exactly what still needs live testing
before you'd call this "done," and do that first since it changes what numbers you
report in the eval section.

## Stack choice: LiveKit Agents + OpenAI Realtime, not Retell/Bolna/Vapi

- **Latency**: speech-to-speech (OpenAI Realtime) skips the ASR-finalize -> LLM ->
  TTS-request serialization that a cascaded pipeline (what Bolna/Retell/Vapi run
  under the hood, even when they call the same OpenAI models) pays on every turn.
  That hop is where code-switched utterances specifically lose time — ASR
  finalization is slower and less confident on mixed Hindi/English segments, and a
  cascaded pipeline can't start LLM inference until ASR commits to a final
  transcript. Speech-to-speech overlaps this.
- **Tool-calling reliability**: needed mid-call, multi-step tool use (lookup ->
  search -> re-search with force_refresh -> book) to hold up over a real
  conversation, not just a single-shot demo call. LiveKit Agents' `function_tool`
  wiring against OpenAI Realtime's native function calling is the combination I have
  the most confidence holds up under retries and partial failures (see
  `agent/tools.py` idempotency keys).
- **Telephony**: LiveKit's SIP trunk integration bridges PSTN calls into a normal
  agent room, so the agent code doesn't need to know it's on a phone call vs. a
  browser test call — this made local testing (browser -> room) and production
  (SIP -> room) the same code path.
- **Multilingual**: gpt-4o-realtime handles Hindi and English and tolerates
  within-utterance code-switching without an explicit language-router step. A
  cascaded platform would need per-segment language detection to route ASR/TTS,
  which is itself a failure point on "Mondays and Wednesdays kaam karega" style
  sentences.
- **Cost**: this is the real trade-off against Retell/Bolna running a cascaded
  GPT-4o-mini pipeline — speech-to-speech is more expensive per minute. For a
  clinic's call volume (front-desk, not a call center), I judged the latency and
  fewer-turns-to-booking payoff worth it. If call volume is high, re-score this:
  a cascaded Bolna pipeline with a fast Hindi-capable ASR (e.g. a
  Whisper-large-v3-based or Sarvam-style ASR) and GPT-4o-mini could cut cost
  substantially at some latency cost — worth an A/B once you have real call volume.

**What I did NOT do**: benchmark all four platforms end-to-end with real audio. This
justification is an architectural argument, not a measured comparison — treat it as
the starting hypothesis the eval harness (once wired to live traffic) should
confirm or overturn.

## Architecture

```
Caller (PSTN/SIP or browser)
        |
   LiveKit Cloud (SIP trunk + room)
        |
   agent/main.py  --  LiveKit Agents worker
        |    (OpenAI Realtime: STT+LLM+TTS in one hop)
        |    tools.py: function_tool wrappers -> HTTP
        v
   backend/ (FastAPI)
        |  app/services/context.py    -- returning patient / callback / dropped-call resume
        |  app/services/scheduling.py -- cross-branch search, conflict-safe booking
        |  app/cliniko_client.py      -- idempotent PMS write-back
        v
   Postgres (source of truth)  +  Redis (availability cache, TTL + write-invalidated)
        |
   Cliniko PMS (downstream write-back, async-safe on failure)
```

Postgres is the source of truth for whether a booking exists. Cliniko is a
downstream sync target — see `app/cliniko_client.py` docstring for the exact
failure-handling contract (local booking always stands; PMS write failures are
logged and retried, never rolled back into "sorry, that didn't work" on the call).

## Required-scenario -> implementation map

| Scenario | Where it's handled |
|---|---|
| Returning patient, no context | `services/context.lookup_patient`, called at call start, injected into system prompt (`prompts.build_call_context_block`) |
| Missed outbound call, callback | `services/context._find_resumable_call` (outbound + no_answer within window) |
| Stale availability from memory | `services/scheduling.search_availability` cache keyed + TTL'd + write-invalidated; agent prompt rule 2 forces `force_refresh=true` before any confirm |
| Earliest-slot across branches/practitioners | `search_availability` fans out over every matching (branch, practitioner) pair concurrently, merges, sorts — no early return |
| Branch-specific triage reliability | `_candidate_practitioners` filters deterministically on branch_id + specialty (ILIKE), no fuzzy/random selection; eval scenario runs it 10x to check consistency |
| Dropped call recovery | `CallSession.transcript_state` snapshot on disconnect + `_find_resumable_call` dropped-call branch |
| Double-booking / conflict resolution | Postgres `EXCLUDE USING gist` constraint on `(practitioner_id, time_range)` — DB-enforced, not app-logic, so it holds under concurrent writes; `book_appointment`/`reschedule_appointment` catch the IntegrityError and return `alternative_slots` |

## Setup

1. **Source your real clinic data.** Replace the placeholder branches/practitioners
   in `eval/fixtures/two_branch_dentistry.sql` (and seed the real ones into Cliniko)
   with an actual clinic's doctors, departments, two branches, and slot durations.
   This assignment requires real data, not invented names — I left placeholders
   because I don't have a clinic to source from here.
2. `cp .env.example .env` and fill in real values. **Rotate the keys you pasted in
   chat first** — they were exposed in plaintext.
3. `docker compose up --build` — brings up Postgres, Redis, the FastAPI backend, and
   the LiveKit agent worker.
4. Run the DB migration once against a fresh database:
   ```
   docker compose exec backend alembic upgrade head
   ```
   (The FastAPI app's startup also runs `create_all` for convenience in dev, but the
   double-booking `EXCLUDE` constraint only exists after this migration.)
5. Point a LiveKit SIP trunk (or a browser test room) at `LIVEKIT_URL` and place a
   call — the agent worker (`agent/main.py`) picks up the job automatically once
   registered with `cli.run_app`.

## Eval harness

```
cd eval
pip install -r requirements.txt
python run_eval.py --all --report results/report.md
```

Scenarios live in `eval/scenarios/*.yaml`, one file per required failure mode plus
the natural-time-reference cases. `metrics.py` defines and justifies each metric
(turns-to-completion, redundant-question rate, per-language task success, latency
by component where components are observable).

**Honest state of the harness right now**: `_agent_harness.py` is a documented stub,
not a fake pass/fail. I chose not to hand you fabricated "92% pass rate" numbers for
a live model I never actually ran — see that file's docstring for exactly what's
left to wire (mainly: extracting `agent/main.py`'s session bootstrap into a
LiveKit-room-independent function so scenarios can be pushed through the same
prompt+tools as text, and a `--mode=voice` path for real ASR/latency numbers). This
is also the single biggest reason the eval harness as-shipped would give you false
confidence if you skipped straight to "green checkmarks == done" — see the
docstring in `metrics.py` for the other two.

## Testing real phone calls (SIP)

Browser testing (LiveKit Agents Playground) needs nothing beyond what's already
running. Real phone calls need a SIP trunk — see `sip/MANUAL_STEPS.md` for the full
walkthrough. Short version: everything scriptable lives in `sip/setup_sip.sh` and
`sip/verify_sip.sh`; everything else (provider signup, buying a number, entering
credentials into that provider's dashboard, dialing the phone) is genuinely manual
and listed step by step in that file.

## Known limitations

- **Not run against live Cliniko/LiveKit/OpenAI.** My sandbox can't reach
  `api.cliniko.com`, `*.livekit.cloud`, or `api.openai.com`, so nothing here has
  been exercised against real APIs. Test the Cliniko client's field names/endpoints
  against your actual Cliniko shard first — API surfaces drift.
- **`_agent_harness.py` is a stub** (see above) — the eval harness structure and
  metrics are real; the "run it and get numbers" wiring is the next step.
- **PMS write-back retry worker isn't scheduled.** Failed Cliniko writes are logged
  to `pms_writeback_log` with `status=failed`, but there's no cron/worker retrying
  them yet — add one (Celery beat, or a simple polling loop) before production.
- **`_free_slots_for_practitioner` is a local open-hours model**, not a live read of
  each practitioner's actual Cliniko calendar (blocked time, holidays, walk-ins
  entered directly in Cliniko). `cliniko_client.available_times` exists and is the
  correct source for that — wiring `search_availability` to prefer it over the local
  computation when a `cliniko_practitioner_id` is set is the natural next step.
- **Dropped-call detection is a heuristic** (`main.py`'s `_on_disconnect`) — it
  currently treats *any* participant disconnect as a drop unless the agent reached
  an explicit terminal state. A cleaner signal (an explicit `call_complete` tool
  call from the agent) is noted inline as the fix.
- **Natural-language time parsing** ("around 4:30", "Mondays and Wednesdays") is
  delegated entirely to the Realtime model's own reasoning against the
  `search_availability` tool schema, per the system prompt rules — there's no
  separate deterministic date-parser as a safety net. Worth adding regex/dateparser
  fallback validation on the extracted `day_of_week`/`time_of_day` args if you see
  the model drift on edge cases like "the 13th" without a month.

LiveKit Telephony agent
-----------------------
This repository includes an example LiveKit telephony agent in [agent/telephony_agent.py](agent/telephony_agent.py). Quick steps to get it running locally:

- Copy `.env.example` -> `.env` and fill in API keys for Deepgram, OpenAI, Cartesia, and LiveKit.
- Create and activate a Python 3.11 virtualenv, then install `agent/requirements.txt`.

```
python -m venv venv
source venv/bin/activate
pip install -r agent/requirements.txt
python agent/telephony_agent.py start
```

If you want me to switch the example to multilingual mode, or to add a small smoke test, tell me which you'd prefer.

## Local backend + worker

Use Python 3.11 or newer. The current LiveKit Agents package does not support
Python 3.9 (`typing.TypeAlias` is unavailable there). From the repository root:

```
source venv/bin/activate
python -m pip install -r backend/requirements.txt
python -m pip install -r agent/requirements.txt
```

Configure `.env` from `.env.example`, then use two terminals:

```
# Terminal 1: backend
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2: LiveKit worker
source venv/bin/activate
python agent/main.py start
```

For Cliniko, set `CLINIKO_SHARD`, `CLINIKO_API_KEY`, and
`CLINIKO_APPOINTMENT_TYPE_ID`. Before enabling writes, populate each local
branch's `cliniko_business_id`, each practitioner's
`cliniko_practitioner_id`, and each existing patient's `external_id` with the
matching Cliniko IDs. The backend then creates an `individual_appointment` and
stores its Cliniko ID for cancellation and rescheduling.

## Docker: backend and worker together

Copy `.env.example` to `.env`, fill in the LiveKit/LLM credentials and replace
`AGENT_SERVICE_TOKEN` with a long random value, then run:

```
docker compose up --build -d
docker compose logs -f backend agent
```

The backend is available at `http://localhost:8000` and its SQLite data is kept
in the named `voiceai-data` Docker volume. The worker uses
`http://backend:8000` internally; do not set `BACKEND_URL` to `localhost` for
the Docker worker. Stop the stack with `docker compose down` (the database
volume is retained).
# voiceai
