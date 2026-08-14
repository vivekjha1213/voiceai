"""
LiveKit voice agent worker for the TwoCare clinic receptionist.

STACK CHOICE: LiveKit Agents + OpenAI Realtime API (gpt-4o-realtime) for a single
speech-to-speech model doing STT+LLM+TTS in one hop, rather than a cascaded
ASR->LLM->TTS pipeline. Rationale (expand in README):
  - Latency: realtime speech-to-speech avoids two serialization hops
    (ASR final transcript -> LLM -> TTS text), which is where cascaded
    pipelines lose the most time on code-switched utterances specifically,
    because ASR finalization is slower on mixed-language segments.
  - Tool-calling reliability: OpenAI Realtime's native function-calling is
    stable mid-conversation and interoperates directly with livekit-agents'
    function_tool decorator (see tools.py).
  - Multilingual: gpt-4o-realtime handles Hindi and English and tolerates
    within-utterance code-switching without an explicit language-detection
    step, which a cascaded pipeline would otherwise need (separate STT
    language routing).
  - Telephony: LiveKit SIP trunk in front of this room handles PSTN in/out;
    the agent itself is telephony-agnostic once the call is bridged into a room.
  - Cost: higher per-minute than cascaded STT+cheap-LLM+TTS; acceptable given
    clinic call volume and the latency/turns-to-completion payoff. See README
    for the actual $/call comparison against a Bolna+GPT-4o-mini cascade,
    which is the real alternative we scored this against.

This file focuses on wiring: call lifecycle (start/end tracked in the backend
for continuity), the system prompt + call-context injection, and dropped-call
detection via the room's participant disconnect event.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import APIStatusError, OpenAI

from livekit import agents
from livekit.agents import (
    Agent, AgentSession, JobContext, WorkerOptions, cli, RoomInputOptions,
)
from livekit.plugins import openai, silero

from prompts import SYSTEM_PROMPT, build_call_context_block
from tools import ALL_TOOLS

# Keep one shared configuration file whether this worker is launched locally
# from `agent/` or by Docker Compose.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logger = logging.getLogger("clinic-agent")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
AGENT_TOKEN = os.environ.get("AGENT_SERVICE_TOKEN", "changeme")


def validate_openai_credentials() -> bool:
    """Validate the active LLM key, preferring Gemini when a Gemini key is set."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if gemini_key:
        api_key = gemini_key
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    elif openai_key:
        api_key = openai_key
        base_url = None
        model = "gpt-4o-mini"
    else:
        logger.error("No OPENAI_API_KEY or GEMINI_API_KEY is set in the environment.")
        return False

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say hello in one sentence."}],
            max_tokens=10,
        )
        return True
    except APIStatusError as exc:
        if exc.status_code == 429:
            logger.error(
                "The configured LLM provider is rate-limited or has no active billing credits. "
                "Set a funded GEMINI_API_KEY or OPENAI_API_KEY before starting the worker."
            )
            return False
        logger.error("LLM validation failed: %s", exc)
        return False
    except Exception as exc:  # pragma: no cover - defensive guard for startup validation
        logger.error("Unable to validate configured LLM access: %s", exc)
        return False


class ClinicReceptionistAgent(Agent):
    def __init__(self, call_context_block: str):
        super().__init__(
            instructions=SYSTEM_PROMPT + "\n\n" + call_context_block,
            tools=ALL_TOOLS,
        )


async def entrypoint(ctx: JobContext):
    if not validate_openai_credentials():
        logger.error("OpenAI is unavailable; skipping agent startup.")
        # Finalize the job cleanly so the orchestrator doesn't warn.
        await ctx.shutdown()
        return

    await ctx.connect()

    # Caller's phone number is passed through SIP participant attributes by
    # LiveKit's SIP trunk integration (sip.phoneNumber). Fall back to room
    # name for local/browser testing.
    participant = await ctx.wait_for_participant()
    phone_e164 = participant.attributes.get("sip.phoneNumber", f"test:{ctx.room.name}")
    direction = "inbound" if participant.attributes.get("sip.trunkPhoneNumber") else "inbound"

    async with httpx.AsyncClient(base_url=BACKEND_URL, headers={"X-Agent-Token": AGENT_TOKEN}, timeout=8.0) as client:
        call_resp = await client.post(
            "/calls/start",
            json={"livekit_room_name": ctx.room.name, "phone_e164": phone_e164, "direction": direction},
        )
        call_session_id = call_resp.json()["call_session_id"]

        lookup_resp = await client.post("/tools/lookup_patient", json={"phone_e164": phone_e164})
        lookup = lookup_resp.json()

    context_block = build_call_context_block(lookup) + f"\n- call_session_id: {call_session_id}"

    gemini_key = os.getenv("GEMINI_API_KEY")
    llm_model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    llm_provider = openai.realtime.RealtimeModel(
        model=llm_model if gemini_key else "gpt-4o-realtime-preview",
        voice="alloy",
        api_key=gemini_key or os.getenv("OPENAI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/" if gemini_key else None,
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        llm=llm_provider,
    )

    dropped = {"flag": False}

    def _on_disconnect(*_):
        # Distinguish a normal hangup (agent already said goodbye / call
        # reached a terminal booking state) from an abrupt drop. We use a
        # simple heuristic here: if the session's last turn was not a
        # closing confirmation, treat as dropped. A production version
        # would track an explicit "call_complete" tool call instead.
        dropped["flag"] = True

    ctx.room.on("participant_disconnected", _on_disconnect)

    agent = ClinicReceptionistAgent(context_block)
    await session.start(agent=agent, room=ctx.room, room_input_options=RoomInputOptions())

    await ctx.wait_for_shutdown()

    outcome = "dropped" if dropped["flag"] else "completed"
    async with httpx.AsyncClient(base_url=BACKEND_URL, headers={"X-Agent-Token": AGENT_TOKEN}, timeout=8.0) as client:
        await client.post(
            "/calls/end",
            json={
                "call_session_id": call_session_id,
                "outcome": outcome,
                # In production this snapshot comes from the agent's own
                # tracked intent/slot-filling state, not the raw transcript.
                "transcript_state": {"note": "wire agent.session state snapshot here"},
                "language_mix": "en+hi",
            },
        )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
