"""Gate 0: minimal LiveKit agent that speaks through Rime and hears through Deepgram.

Verifies the shipped path: plugin wiring, barge-in, word-timestamped Rime output,
manual turn mode switching. No coaching logic here.

Console mode (local mic/speakers, no LiveKit server needed):
    .venv/Scripts/python.exe agent/gate0_agent.py console
Room mode (needs LIVEKIT_* in .env and a browser client):
    .venv/Scripts/python.exe agent/gate0_agent.py dev
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, TurnHandlingOptions, function_tool
from livekit.plugins import deepgram, rime, silero

from agent.llm_config import COACH_MODEL, coach_llm

load_dotenv()
log = logging.getLogger("gate0")

RIME_MODEL = os.environ.get("RIME_MODEL", "mistv3")
RIME_SPEAKER = os.environ.get("RIME_SPEAKER", "summit")


class Gate0(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are Podium, a presentation coach. Keep every reply under two short sentences. "
                "If the user says they want to present, call start_presentation. "
                "Speak plainly; no lists, no markdown."
            )
        )

    @function_tool()
    async def start_presentation(self, context: agents.RunContext) -> str:
        """Switch to presentation mode: stop responding to pauses until the user says they are done."""
        session = context.session
        session.update_options(turn_detection="manual")
        log.info("turn_detection -> manual")
        return "Presentation mode on. Say 'I'm done' when finished."


server = agents.AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext) -> None:
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en", filler_words=True),
        llm=coach_llm(),
        tts=rime.TTS(model=RIME_MODEL, speaker=RIME_SPEAKER, lang="eng",
                     sample_rate=24000, use_websocket=True,
                     pause_between_brackets=(RIME_MODEL != "coda")),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(turn_detection="vad"),
        use_tts_aligned_transcript=True,
    )

    @session.on("user_input_transcribed")
    def _on_user(ev: agents.UserInputTranscribedEvent) -> None:
        if ev.is_final:
            log.info("USER: %s", ev.transcript)
            if session.turn_detection == "manual" and "done" in ev.transcript.lower():
                log.info("manual commit_user_turn()")
                session.update_options(turn_detection="vad")
                session.commit_user_turn()

    @session.on("agent_state_changed")
    def _on_state(ev: agents.AgentStateChangedEvent) -> None:
        log.info("agent %s -> %s", ev.old_state, ev.new_state)

    await session.start(room=ctx.room, agent=Gate0())
    await session.say(
        f"Hi, I'm Podium, speaking through Rime {RIME_MODEL} as {RIME_SPEAKER}. <400> Say 'let me present' to try presentation mode.",
        allow_interruptions=True,
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
