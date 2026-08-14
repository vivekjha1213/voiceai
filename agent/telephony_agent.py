import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import cartesia, deepgram, openai, silero

from tools import ALL_TOOLS


# ============================================================
# Environment
# ============================================================

# Keep one shared configuration file whether this worker is launched locally
# from `agent/` or by Docker Compose.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("telephony-agent")


# ============================================================
# Gemini validation
# ============================================================

def validate_gemini_credentials() -> bool:
    """
    Validate Gemini through Google's OpenAI-compatible API.
    """

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        logger.error("GEMINI_API_KEY is missing.")
        return False

    model = os.getenv(
        "GEMINI_MODEL",
        "gemini-3.1-flash-lite",
    )

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=(
                "https://generativelanguage.googleapis.com/"
                "v1beta/openai/"
            ),
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Reply with only: OK",
                }
            ],
            max_tokens=5,
        )

        result = response.choices[0].message.content

        logger.info(
            "Gemini API validation successful: %s",
            result,
        )

        return True

    except Exception as exc:
        logger.error(
            "Gemini API validation failed: %s",
            exc,
            exc_info=True,
        )
        return False


# ============================================================
# Current time tool
# ============================================================

@function_tool
async def get_current_time() -> str:
    """Return the current local server time."""

    return datetime.now().strftime("%I:%M %p")


# ============================================================
# Main LiveKit entrypoint
# ============================================================

async def entrypoint(ctx: JobContext):
    """
    Main entry point for the TwoCare telephony agent.
    """

    logger.info("Starting TwoCare telephony agent.")

    # --------------------------------------------------------
    # Validate Gemini
    # --------------------------------------------------------

    if not validate_gemini_credentials():
        logger.error(
            "Gemini is unavailable; cannot start the voice agent."
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Connect to LiveKit
    # --------------------------------------------------------

    try:
        await ctx.connect()

        logger.info(
            "Connected to LiveKit room: %s",
            ctx.room.name,
        )

    except Exception as exc:
        logger.error(
            "LiveKit connection failed: %s",
            exc,
            exc_info=True,
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Wait for caller
    # --------------------------------------------------------

    try:
        participant = await ctx.wait_for_participant()

        logger.info(
            "Phone call connected from participant: %s",
            participant.identity,
        )

    except Exception as exc:
        logger.error(
            "Failed waiting for participant: %s",
            exc,
            exc_info=True,
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    # ========================================================
    # Agent instructions
    # ========================================================

    agent = Agent(
        instructions="""
You are TwoCare's friendly and professional AI clinic assistant.

You answer inbound phone calls for a healthcare clinic.

LANGUAGE
- Support English and Hindi.
- Support natural mid-call English/Hindi code-switching.
- Respond in the language the caller is currently using.
- If the caller mixes Hindi and English, naturally respond in a similar
  mixed-language style when appropriate.
- Keep responses concise and easy to understand over a phone call.

GENERAL BEHAVIOR
- Be warm, professional and helpful.
- Keep spoken responses generally under 30 seconds.
- Ask one question at a time.
- Never fabricate patient information.
- Never fabricate doctor information.
- Never fabricate availability.
- Never fabricate appointment slots.
- Never claim an appointment was booked unless the booking tool confirms it.
- Never claim a cancellation succeeded unless the tool confirms it.
- Never claim a rescheduling succeeded unless the tool confirms it.

PATIENT INFORMATION
When patient information is needed, use:

lookup_patient

Do not invent:
- patient ID
- patient name
- phone number
- appointment history

AVAILABILITY
NEVER guess availability.

Whenever the caller asks about appointment availability,
use:

search_availability

Natural-language examples:

"Do you have anything on December 13 around 1?"

Resolve the date and approximate time, then call
search_availability.

"Mondays and Wednesdays work for me."

Use:
day_of_week = Monday/Wednesday

and an appropriate upcoming date range.

"Any Thursday morning is fine."

Use:
day_of_week = Thursday
time_of_day = morning

If a phrase is ambiguous:

"afternoon"

ask a short clarification such as:

"Do you prefer earlier afternoon or around 4:30 PM?"

Then call search_availability.

FORCE REFRESH
When checking a specific appointment slot immediately before booking,
use:

force_refresh=true

If the availability changed, tell the caller and offer alternatives.

NO AVAILABILITY
If search_availability returns no slots:

- Do not invent a slot.
- Offer the nearest available alternatives.
- Explain what changed:
  date, time, practitioner, or branch.

BOOKING
Before booking:

1. Identify/confirm the patient.
2. Confirm practitioner if necessary.
3. Confirm date.
4. Confirm exact time.
5. Confirm the appointment slot.
6. Use book_appointment.

Only say:

"Your appointment is booked"

after book_appointment confirms success.

RESCHEDULING
Use:

reschedule_appointment

Confirm the new appointment details.

CANCELLATION
Use:

cancel_appointment

Confirm the appointment being cancelled as required.

TOOLS
Use the supplied tools:

lookup_patient
search_availability
book_appointment
reschedule_appointment
cancel_appointment

Also use:

get_current_time

Do not emulate the backend.
Do not fabricate backend results.
Always use the real tools for patient and appointment information.

CONVERSATION STYLE
- Friendly
- Professional
- Concise
- Natural for a phone conversation
- Ask one question at a time

GREETING

Start with a short greeting.

Example:

"Good evening! Thank you for calling TwoCare. How may I help you today?"
""",
        tools=[
            get_current_time,
            *ALL_TOOLS,
        ],
    )

    # ========================================================
    # Text-to-Speech
    # ========================================================

    tts_provider = None

    cartesia_key = os.getenv("CARTESIA_API_KEY")

    if cartesia_key:
        try:
            # Create two provider instances for English and Hindi. We'll wrap them
            # into a simple multilingual adapter that chooses voice per-turn.
            tts_en = cartesia.TTS(
                model="sonic-2",
                voice="a0e99841-438c-4a64-b679-ae501e7d6091",
                language="en",
                sample_rate=24000,
            )
            # Hindi voice selection is optional; re-use the same model if a Hindi
            # voice is not known. Replace voice id with a Hindi-capable voice if available.
            tts_hi = cartesia.TTS(
                model="sonic-2",
                voice="a0e99841-438c-4a64-b679-ae501e7d6091",
                language="hi",
                sample_rate=24000,
            )

            class MultilingualTTS:
                def __init__(self, en, hi):
                    self.en = en
                    self.hi = hi

                # The livekit-agents library expects a TTS provider exposing
                # a speak(text, **kwargs) or similar method. We provide a
                # `speak` wrapper that chooses a provider based on `language`.
                def speak(self, text: str, language: str | None = None, **kwargs):
                    lang = (language or "en").lower()
                    provider = self.hi if lang.startswith("hi") or lang.startswith("hi") else self.en
                    # delegate to provider. If provider expects different args,
                    # the adapter may need adjustment for your Cartesia SDK.
                    return provider.speak(text, language=lang, **kwargs)

            tts_provider = MultilingualTTS(tts_en, tts_hi)

            logger.info(
                "Using Cartesia TTS provider."
            )

        except Exception as exc:
            logger.warning(
                "Cartesia TTS unavailable: %s",
                exc,
            )

    if tts_provider is None:
        logger.error(
            "No TTS provider available."
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    # ========================================================
    # Gemini LLM
    # ========================================================

    gemini_key = os.getenv("GEMINI_API_KEY")

    if not gemini_key:
        logger.error(
            "GEMINI_API_KEY is missing."
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    gemini_model = os.getenv(
        "GEMINI_MODEL",
        "gemini-3.5-flash",
    )

    llm_provider = openai.LLM(
        model=gemini_model,
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/"
        ),
        temperature=0.7,
    )

    logger.info(
        "Using Gemini LLM: %s",
        gemini_model,
    )

    # ========================================================
    # Agent session
    # ========================================================

    session = AgentSession(
        # Voice Activity Detection
        vad=silero.VAD.load(),

        # Speech-to-Text
        # multi = English/Hindi/multilingual input
        stt=deepgram.STT(
            model="nova-3",
            language="multi",
            interim_results=True,
            punctuate=True,
            smart_format=True,
            filler_words=True,
            endpointing_ms=25,
            sample_rate=16000,
        ),

        # Gemini
        llm=llm_provider,

        # Cartesia
        tts=tts_provider,
    )

    # ========================================================
    # START SESSION
    # ========================================================

    try:
        await session.start(
            agent=agent,
            room=ctx.room,
        )

        logger.info(
            "Agent session started successfully."
        )

    except Exception as exc:
        logger.error(
            "session.start() failed: %s",
            exc,
            exc_info=True,
        )

        try:
            await ctx.shutdown()
        except Exception:
            pass

        return

    logger.info(
        "Agent is ready and waiting for caller speech."
    )


# ============================================================
# Worker startup
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s - "
            "%(name)s - "
            "%(levelname)s - "
            "%(message)s"
        ),
    )

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="telephony_agent",
        )
    )
