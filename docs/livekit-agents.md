# LiveKit Agents Python 1.x — Verified API Facts (Podium)

Researched 2026-09-07 from official docs (docs.livekit.io, content-negotiated `.md`) and GitHub source (`github.com/livekit/agents` @ `main`). Everything is VERIFIED against the cited URL unless marked **[UNVERIFIED]**.

**Current version: `livekit-agents 1.8.0`** on PyPI (Python `>=3.10,<3.15`; pulls `livekit==1.1.17`, `livekit-api<2,>=1.2.0`). URL: https://pypi.org/pypi/livekit-agents/json (`"version": "1.8.0"`).

---

## 1. Version, minimal AgentSession, CLI entrypoint, .env

**Minimal AgentSession (official quickstart)** — https://docs.livekit.io/agents/start/voice-ai.md:

```python
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, inference, room_io, TurnHandlingOptions
from livekit.plugins import rime, deepgram
load_dotenv(".env.local")

class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a helpful voice AI assistant.")

server = AgentServer()

@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: agents.JobContext):
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en"),
        llm=inference.LLM(model="google/gemma-4-31b-it"),   # or any llm.LLM
        tts=rime.TTS(model="coda", speaker="celeste"),
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()),
    )
    await session.start(room=ctx.room, agent=Assistant(), room_options=room_io.RoomOptions(...))
    await session.generate_reply(instructions="Greet the user and offer your assistance.")

if __name__ == "__main__":
    agents.cli.run_app(server)
```

**Legacy alternate entrypoint** — `agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))` still shown in the official PyPI README description: https://pypi.org/pypi/livekit-agents/json

`AgentSession.__init__` kwargs verified from source (`livekit-agents/livekit/agents/voice/agent_session.py`): `stt, vad, llm, tts, turn_handling, tools, tool_handling, max_tool_steps=3, use_tts_aligned_transcript, tts_text_transforms, userdata, user_away_timeout=15.0, ...`. Default VAD when omitted: `inference.VAD(model="silero")`.

**Startup modes** — https://docs.livekit.io/agents/server/startup-modes.md:
- `dev` — debug logging, auto-reload (`--no-reload`); `lk agent dev` or `python src/agent.py dev`.
- `start` — production, graceful drain on SIGINT/SIGTERM, `--log-level` (default `info`).
- `console` — single-session local; `--text`, `--input-device`, `--output-device`, `--list-devices`, `--record` (saves to `console-recordings/session-<timestamp>/`). Does not connect to LiveKit.
- `connect` — `uv run src/agent.py connect --room my-test-room`.

**.env** (Authentication table, same URL): `LIVEKIT_URL` (from `--url`), `LIVEKIT_API_KEY` (from `--api-key`), `LIVEKIT_API_SECRET` (from `--api-secret`). Provider keys: `DEEPGRAM_API_KEY`, `RIME_API_KEY`, `OPENAI_API_KEY`. `lk app env -w` writes `.env.local`.

---

## 2. Turn detection: manual mode, commit/clear/interrupt, runtime switching, endpointing

**Modes** — https://docs.livekit.io/reference/agents/turn-handling-options.md: `TurnDetector()` (default audio model), `"stt"`, `"vad"`, `"realtime_llm"`, `"manual"`.

**Manual / push-to-talk** — https://docs.livekit.io/agents/build/turns.md:

```python
session = AgentSession(turn_handling=TurnHandlingOptions(turn_detection="manual"))
session.input.set_audio_enabled(False)

@ctx.room.local_participant.register_rpc_method("start_turn")
async def start_turn(data: rtc.RpcInvocationData):
    session.interrupt(); session.clear_user_turn(); session.input.set_audio_enabled(True)

@ctx.room.local_participant.register_rpc_method("end_turn")
async def end_turn(data: rtc.RpcInvocationData):
    session.input.set_audio_enabled(False); session.commit_user_turn()
```

**APIs from source** (`voice/agent_session.py` lines 1522-1608):
- `session.interrupt(*, force: bool = False) -> asyncio.Future[None]`
- `session.clear_user_turn() -> None`
- `session.commit_user_turn(*, transcript_timeout: float = 2.0, stt_flush_duration: float = 2.0, skip_reply: bool = False) -> asyncio.Future[str]` — Python-only; future resolves with the user transcript; `skip_reply=True` commits without a reply.
- Empty-turn guard: `on_user_turn_completed` → `raise StopResponse()` when `new_message.text_content` empty (docs, same page).

**Runtime switching** — `session.update_options(*, endpointing_opts, turn_detection: TurnDetectionMode | None, keyterms, expressive, ...)` (source lines 1322-1391; `turn_detection=None` reverts to auto). Docs corroboration: https://docs.livekit.io/reference/agents/turn-handling-options.md ("Update endpointing at runtime").

**Endpointing** — `EndpointingOptions{ mode: "fixed"|"dynamic" (default "fixed"), min_delay (0.5s; 0.3s with audio turn detector), max_delay (3.0s; 2.5s with audio turn detector), alpha (0.9, dynamic only) }`, Python in seconds. Legacy `AgentSession(min_endpointing_delay=..., max_endpointing_delay=...)` params still exist but are deprecated (source `deprecate_params`, removal target v2.0).

---

## 3. session.say / generate_reply, SpeechHandle, interruption truncation

**`session.say`** — https://docs.livekit.io/agents/build/audio.md; source (agent_session.py lines 1418-1425):

```python
def say(self, text: str | AsyncIterable[str], *,
        audio: NotGivenOr[AsyncIterable[rtc.AudioFrame]] = NOT_GIVEN,
        allow_interruptions: NotGivenOr[bool] = NOT_GIVEN,
        add_to_chat_ctx: bool = True) -> SpeechHandle
```
`text` added to transcript/chat ctx unless `add_to_chat_ctx=False`; `audio`-only playback adds nothing to transcript. Returns `SpeechHandle`; triggers `speech_created`.

**`session.generate_reply`** — source lines 1452-1462:

```python
def generate_reply(self, *, user_input=NOT_GIVEN, instructions=NOT_GIVEN,
                   tool_choice=NOT_GIVEN, tools: list[str] | None = None,
                   allow_interruptions=NOT_GIVEN, chat_ctx=NOT_GIVEN,
                   input_modality: Literal["text", "audio"] = "text") -> SpeechHandle
```
Docs parameter reference: https://docs.livekit.io/agents/build/audio.md. Inside a function tool, `tool_choice` defaults to `"none"`.

**SpeechHandle** — source: https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/voice/speech_handle.py:
- `interrupted: bool` (property), `done() -> bool`, `interrupt(*, force=False) -> SpeechHandle`
- `await handle` / `await handle.wait_for_playout()` (waits for full playout incl. tool calls & follow-ups; calling it from the tool that owns the handle raises — use `RunContext.wait_for_playout()` there)
- `add_done_callback(cb: Callable[[SpeechHandle], None])`, `remove_done_callback`, `exception() -> BaseException | None` (await never raises — check this instead), `chat_items`, `id`, `num_steps`, `allow_interruptions` (settable)

```python
handle = session.say("Hello world")
handle.add_done_callback(lambda _: print("speech done"))
if handle.interrupted: web_request.cancel()
```

**Truncation on interruption** — https://docs.livekit.io/agents/build/turns.md (§ Interruptions): "the agent stops speaking and automatically truncates its conversation history to include only the portion of the speech that the user heard before interruption." Frontend transcription sync also truncates on interruption (https://docs.livekit.io/agents/multimodality/text.md). Mechanism: `SpeechHandle._chat_items` records only items generated during playout (source).

---

## 4. Function tools

**Decorator** — https://docs.livekit.io/agents/logic/tools/definition.md:

```python
from livekit.agents import function_tool, Agent, RunContext

class MyAgent(Agent):
    @function_tool()   # or @function_tool(name=..., description=..., flags=ToolFlag.CANCELLABLE)
    async def lookup_weather(self, context: RunContext, location: str) -> dict[str, Any]:
        """Look up weather information for a given location.

        Args:
            location: The location to look up weather information for.
        """
        return {"weather": "sunny", "temperature_f": 70}
```
- Decorator params: `name`, `description` (default docstring), `raw_schema` (raw JSON schema), `flags` (`ToolFlag.NONE | IGNORE_ON_ENTER | CANCELLABLE`). Args inferred from signature + docstring `Args:`. Return auto-stringified; `None` ⇒ silent completion (no LLM reply). Return `(SomeAgent(), "msg")` for handoff. Async directly supported.

**Speaking while a tool runs** — https://docs.livekit.io/agents/logic/tools/async.md:
- `await ctx.update(message)` — non-blocking progress: adds to chat ctx, LLM voices it; tool becomes async on first `ctx.update()`.
- `ctx.with_filler(text, delay=5, interval=None, max_steps=None)` — async CM; plays filler via TTS after idle `delay`, bypassing the LLM; callable `source` for rotating fillers.

**Cancellation / fencing**:
- `ToolFlag.CANCELLABLE` lets the LLM cancel a running tool (definition.md Tool flags; https://docs.livekit.io/agents/logic/tools/async.md#cancellation).
- `RunContext.wait_for_playout()` — safe way to await agent speech inside a tool.
- `FunctionToolsExecutedEvent.function_call_outputs[i].reply_required` — built-in fencing: outputs from tools finished after user interruption / `StopResponse` / empty returns get `reply_required=False` (no auto reply); also `zipped()` and `cancel_tool_reply()` — https://docs.livekit.io/agents/build/events.md (§ function_tools_executed; type change in v1.7.0).

---

## 5. AgentSession events

Payload shapes from https://docs.livekit.io/agents/build/events.md:
- **user_input_transcribed** → `UserInputTranscribedEvent{ transcript: str, is_final: bool, speaker_id: str | None, language: str | None }`
- **user_state_changed** → `UserStateChangedEvent{ old_state, new_state }`; states `speaking | listening | away` (away after `user_away_timeout=15.0` default)
- **agent_state_changed** → `AgentStateChangedEvent{ old_state, new_state }`; states `initializing | idle | listening | thinking | speaking`
- **conversation_item_added** → `ConversationItemAddedEvent{ item: ChatMessage }`; `item.role/text_content/interrupted/content (str | ImageContent | AudioContent)/metrics (per-turn latency)`
- **speech_created** → `SpeechCreatedEvent{ user_initiated: bool, source: "say"|"generate_reply"|"tool_response", speech_handle }`
- **metrics_collected** → session-level **deprecated** (use `session_usage_updated` + `ChatMessage.metrics`); `MetricsCollectedEvent{ metrics: STTMetrics | LLMMetrics | TTSMetrics | VADMetrics | EOUMetrics | InterruptionMetrics }`. Per-plugin `metrics_collected` is NOT deprecated.
- Others: `error` (ErrorEvent{error.recoverable, source}), `close` (CloseEvent{error, reason}), `function_tools_executed`, `session_usage_updated`, `user_interruption_detected`, `agent_false_interruption`, `overlapping_speech`.

**STT word timestamps & confidence — YES, framework-native** (source https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/stt/stt.py):

```python
@dataclass
class SpeechData:
    language: LanguageCode; text: str
    start_time: float = 0.0; end_time: float = 0.0
    confidence: float = 0.0        # [0, 1]
    speaker_id: str | None = None
    words: list[TimedString] | None = None   # TimedString: text, start_time, end_time

@dataclass
class SpeechEvent:
    type: SpeechEventType   # START_OF_SPEECH | INTERIM_TRANSCRIPT | PREFLIGHT_TRANSCRIPT
                            # | FINAL_TRANSCRIPT | RECOGNITION_USAGE | END_OF_SPEECH
    request_id: str = ""
    alternatives: list[SpeechData] = ...
```
Deepgram plugin declares `STTCapabilities(aligned_transcript="word")` and populates `SpeechData.words` from Deepgram's `alt["words"]` with per-word `start`/`end` (`punctuated_word` used when punctuate/smart_format); `confidence` = `alt["confidence"]` — source `livekit-plugins/livekit-plugins-deepgram/livekit/plugins/deepgram/stt.py` (`live_transcription_to_speech_data`, lines ~908-949). Consume via `stt.stream()` SpeechEvents; in-session text-level only via `user_input_transcribed`.

---

## 6. Deepgram plugin

Constructor verified from source: https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-deepgram/livekit/plugins/deepgram/stt.py

```python
deepgram.STT(*, model="nova-3", language="en-US", detect_language=False,
    interim_results=True,           # interim results ON by default
    punctuate=True, smart_format=False, sample_rate=16000, no_delay=True,
    endpointing_ms=25,              # 0 disables
    enable_diarization=False,
    filler_words=True,              # YES — exposed; ON by default (improves turn-detector accuracy)
    keywords=NOT_GIVEN,             # (keyword, boost) tuples — NOT supported on Nova-3
    keyterm=NOT_GIVEN,              # str | list[str] — Nova-3 only
    tags=NOT_GIVEN, profanity_filter=False, redact=NOT_GIVEN,
    api_key=NOT_GIVEN,              # else DEEPGRAM_API_KEY env
    http_session=None, base_url="https://api.deepgram.com/v1/listen",
    numerals=False, mip_opt_out=False, vad_events=True,
    utterance_end_ms=None,          # requires interim_results
    dictation=False, replace=None, search=None)
```
- `filler_words` is sent to Deepgram as `"filler_words"` in the WS config (source `_connect_ws`). `stt.update_options(**kwargs incl. filler_words=)` works mid-session (triggers reconnect).
- Flux: `deepgram.STTv2(model="flux-general-en", eager_eot_threshold=0.4)`; `STTv2.update_options(eot_threshold=..., keyterm=...)` — https://docs.livekit.io/agents/models/stt/deepgram.md. Install: `uv add "livekit-agents[deepgram]~=1.5"`.

---

## 7. Rime plugin

Source: https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-rime/livekit/plugins/rime/tts.py; docs: https://docs.livekit.io/agents/models/tts/rime/

```python
from livekit.plugins import rime
rime.TTS(*,
    base_url=NOT_GIVEN,        # default https://users.rime.ai/v1/rime-tts; ws://|wss:// implies use_websocket
    model=NOT_GIVEN,           # default "coda"; models Literal["mistv2", "mistv3", "coda"] (source models.py)
    speaker=NOT_GIVEN,         # coda -> "lyra" default; no-model -> "astra"
    lang="eng",
    speed_alpha=NOT_GIVEN,     # all models; ONLY speed control over WebSocket
    time_scale_factor=NOT_GIVEN,  # HTTP only; coda & mistv3 only (ValueError on mistv2)
    sample_rate=22050,         # source default 22050; docs page says 16000 [UNVERIFIED which is in release]
    reduce_latency=..., pause_between_brackets=..., phonemize_between_brackets=...,  # mist
    repetition_penalty=..., temperature=..., top_p=..., max_tokens=...,             # coda
    api_key=NOT_GIVEN,         # else RIME_API_KEY env (docs: Set RIME_API_KEY in your .env)
    http_session=None,
    use_websocket=False,       # True => streaming + word-level timestamps (aligned_transcript=True)
    segment=NOT_GIVEN,         # WS: "bySentence" (default) | "immediate" | "never"
    tokenizer=NOT_GIVEN)
```
- **Word timestamps: YES over WebSocket** — docs: "Set `use_websocket=True` ... WebSocket streaming ... emits word-level timestamps for TTS-aligned transcriptions. Streaming and aligned transcripts are enabled automatically when WebSocket mode is active." Source: WS `"timestamps"` messages -> `push_timed_transcript(TimedString(text, start_time, end_time))`.
- Session wiring: `AgentSession(use_tts_aligned_transcript=True)` + agent `transcription_node` yields `TimedString{start_time, end_time}` (experimental); optional `RoomOptions(text_output=TextOutputOptions(json_format=True))` publishes `{"text", "start_time", "end_time"}` — https://docs.livekit.io/agents/multimodality/text.md.
- `audio_format`: docs page lists `audio_format` (TTSEncoding, default `pcm`, values `pcm`/`mp3`), but current `main` source has **no** `audio_format` ctor arg (WS always `audioFormat=pcm`) **[UNVERIFIED for the released version]**.
- Coda ignores `reduce_latency`, `pause_between_brackets`, `phonemize_between_brackets`, `temperature`, `top_p`, `repetition_penalty` (docs note).
- Retired model: `arcana` (warned in source; docs: retired 2026-08-19, use coda).

---

## 8. OpenAI plugin with custom base_url

Source: https://github.com/livekit/agents/blob/main/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/llm.py; docs: https://docs.livekit.io/agents/models/llm/openai.md

```python
openai.LLM(*, model="gpt-4.1", api_key=NOT_GIVEN,    # else OPENAI_API_KEY
           base_url=NOT_GIVEN, client: openai.AsyncClient | None = None,
           temperature=..., tool_choice=..., max_completion_tokens=...,
           reasoning_effort=..., extra_body=..., extra_headers=..., extra_query=...,
           timeout=..., max_retries=...)
```
- `base_url`/`api_key` pass straight into `openai.AsyncClient` (source) — use `openai.LLM` (Chat Completions mode) for OpenAI-compatible gateways; `openai.responses.LLM` is recommended for direct OpenAI (Responses API).
- `with_*` helpers (docs): `with_azure`, `with_fireworks`, `with_groq`, `with_perplexity`, `with_telnyx`, `with_together`, `with_x_ai`, `with_deepseek` (+ source also `with_cerebras`). "The `with_*()` methods automatically configure the correct API mode."
- Structured output: documented pattern at https://docs.livekit.io/agents/logic/tools/definition.md#structured-output (LLM streams JSON with TTS-style directives + spoken text, parsed in agent). **[UNVERIFIED: exact `response_format` plumbing example for `openai.LLM` chat-completions]**

---

## 9. Frontend data path (agent <-> browser)

Python agent -> room (verified: https://docs.livekit.io/agents/build/session.md, https://docs.livekit.io/transport/data/text-streams.md, https://docs.livekit.io/transport/data/rpc.md, https://docs.livekit.io/transport/data/packets.md):

```python
await room.local_participant.send_text(json.dumps(payload), topic="coach-feedback")
writer = await room.local_participant.stream_text(topic="coach-stream")
await writer.write(chunk); await writer.close()

await room.local_participant.publish_data(json.dumps(payload).encode(),
    reliable=True, destination_identities=["presenter"], topic="coach")  # reliable <=15KiB

@room.local_participant.register_rpc_method("start_turn")
async def start_turn(data: rtc.RpcInvocationData): ...   # data.caller_identity, data.payload; return str

response = await room.local_participant.perform_rpc(
    destination_identity="presenter", method="getUserMetrics", payload="{}")
```

Browser (livekit-client JS):
- Text streams: `room.registerTextStreamHandler('my-topic', async (reader, participantInfo) => { const text = await reader.readAll(); })` — reader.info: `id, topic, timestamp, size, attributes, destinationIdentities`. Mid-stream joiners get nothing. (text-streams.md)
- Data packets: `room.on(RoomEvent.DataReceived, (payload: Uint8Array, participant, kind) => ...)`; publish `room.localParticipant.publishData(data, {reliable, destinationIdentities, topic})` (packets.md).
- RPC: `room.registerRpcMethod('greet', async (data: RpcInvocationData) => {...})`; `await localParticipant.performRpc({destinationIdentity, method, payload})` (rpc.md).

Transcriptions in browser — https://docs.livekit.io/agents/multimodality/text.md:
- Published to the **`lk.transcription` text-stream topic** by default (user STT + agent speech, word-synced); attributes `lk.transcribed_track_id`, `lk.segment_id`, `lk.transcription_final` ("true" on final segment stream).
- Receive: `room.registerTextStreamHandler('lk.transcription', async (reader, participantInfo) => { ... })` (JS snippet in docs); React: `useTranscriptions` hook.
- **`RoomEvent.TranscriptionReceived` and `publish_transcription()` are DEPRECATED** (separate delivery mechanism; `lk.transcription` streams do NOT trigger it). Migration: https://docs.livekit.io/reference/migration-guides/v0-migration/python.md#transcriptions. Disable with `RoomOptions(text_output=False)`.

---

## 10. Raw user audio frames in the agent (save presentation WAV)

Official pattern — https://docs.livekit.io/agents/build/session.md:

```python
async def do_something(track: rtc.RemoteAudioTrack):
    audio_stream = rtc.AudioStream(track)
    async for event in audio_stream:
        ...  # event.frame: rtc.AudioFrame (16-bit linear PCM)
    await audio_stream.aclose()

@server.rtc_session()
async def entrypoint(ctx: agents.JobContext):
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(do_something(track))
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
```
- Frames are 16-bit linear PCM; `frame.sample_rate/num_channels/samples_per_channel` available per frame **[UNVERIFIED: exact field list — standard livekit-rtc AudioFrame API]**; write WAV via `AudioByteStream`/`av`.
- `session.input.audio` is a settable property (the framework itself wraps it for `RecorderIO`, agent_session.py ~1023-1027) — usable to tee/inject audio **[UNVERIFIED as a public API beyond the framework's own use]**.

Feeding a WAV as user audio (testing):
- First-class test framework is **text-only**: `await session.run(user_input="Hello")` (source signature `AgentSession.run(*, user_input: str, input_modality: Literal["text","audio"] = "text", ...)`); https://docs.livekit.io/agents/start/testing.md — "Testing does not make a LiveKit room connection."
- Console mode has **no** `--input <file>` flag (only `--input-device`/`--output-device`/`--text`/`--record`) — https://docs.livekit.io/agents/server/startup-modes.md.
- `livekit.agents.utils.audio.audio_frames_from_file(path)` exists (used in docs error-fallback example, https://docs.livekit.io/agents/build/events.md) and yields frames from an audio file — pipeable into a custom input **[UNVERIFIED: no documented one-liner for WAV injection into AgentSession; supported paths are the text test framework and third-party audio E2E tools (Bluejay/Cekura/Coval/Hamming)]**.
- `livekit.agents.testing` module: **not found** in the `livekit-agents/livekit/agents/` tree on `main` **[UNVERIFIED: current 1.x testing lives in `voice.run_result` / `session.run`]**.

---

## 11. Local LiveKit server on Windows + tokens

- Windows install: "Download the latest release here" -> https://github.com/livekit/livekit/releases/latest (prebuilt `livekit-server_windows_amd64.zip`) — https://docs.livekit.io/transport/self-hosting/local.md.
- `livekit-server --dev` => `API key: devkey`, `API secret: secret`; binds `127.0.0.1:7880` (`--bind 0.0.0.0` for LAN) — same page. Then `.env`: `LIVEKIT_URL=ws://localhost:7880`, `LIVEKIT_API_KEY=devkey`, `LIVEKIT_API_SECRET=secret`.
- LiveKit Cloud free tier: free project w/ agent deployment, inference, media transport — https://docs.livekit.io/agents/start/voice-ai.md. Enhanced noise cancellation is Cloud-only; remove it for self-hosting (same page).
- Tokens with `livekit-api` — source: https://github.com/livekit/python-sdks/blob/main/livekit-api/livekit/api/access_token.py (old `livekit/livekit-server-sdk-python` is ARCHIVED, verified via https://api.github.com/repos/livekit/livekit-server-sdk-python):

```python
from livekit.api import AccessToken, VideoGrants

token = AccessToken(api_key="devkey", api_secret="secret") \
    .with_identity("presenter") \
    .with_grants(VideoGrants(room_join=True, room="podium", can_publish=True, can_subscribe=True))
jwt = token.to_jwt()
```
- Methods (source): `with_ttl(timedelta)` (default 6h), `with_grants(VideoGrants)`, `with_identity`, `with_kind("standard"|"agent"|...)`, `with_name`, `with_metadata`, `with_attributes`, `with_room_config`, `with_room_preset`, `to_jwt()` (HS256); `TokenVerifier` also available. Key/secret default to `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` env vars.

---

## 12. Recording/playback speed at runtime (Rime)

**YES — `rime.TTS.update_options(speed_alpha=...)` exists** (source: rime/tts.py `update_options`, lines 334-409):

```python
tts = rime.TTS(model="coda", speaker="celeste", use_websocket=True)
...
tts.update_options(speed_alpha=1.2)   # other kwargs: model, speaker, lang, sample_rate,
                                      # time_scale_factor (coda/mistv3, HTTP only), temperature,
                                      # top_p, max_tokens, repetition_penalty, reduce_latency,
                                      # pause_between_brackets, phonemize_between_brackets, base_url
```
- Semantics (LiveKit docs): "**Lower than 1.0 results in faster speech while higher than 1.0 results in slower speech**" — https://docs.livekit.io/agents/models/tts/rime/ (inverse of intuitive naming; double-check against https://docs.rime.ai/api-reference/models before shipping).
- `speed_alpha` is the only speed control over WebSocket; `time_scale_factor` is HTTP-only, coda/mistv3 only, raises on mistv2 (docs + source `_check_time_scale_factor_supported`). WS connection pool is invalidated when URL-affecting params change (source).

---

## 13. LiveKit Inference (`livekit.agents.inference`)

Source: https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/inference/stt.py; docs hub: https://docs.livekit.io/agents/models/inference.md (linked from every model page).

**Constructor (STT)** — exact, from source:

```python
inference.STT(model: STTModels | str = NOT_GIVEN, *,       # "provider/model[:language]"
    language: str | None = None,
    base_url=NOT_GIVEN,      # else LIVEKIT_URL
    encoding="pcm_s16le", sample_rate=16000,
    api_key=NOT_GIVEN,       # else LIVEKIT_INFERENCE_API_KEY, else LIVEKIT_API_KEY
    api_secret=NOT_GIVEN,    # else LIVEKIT_INFERENCE_API_SECRET, else LIVEKIT_API_SECRET
    http_session=None,
    extra_kwargs=NOT_GIVEN,  # provider-specific options (typed per provider)
    fallback=NOT_GIVEN,      # "provider/model" or {model: ..., extra_kwargs: {...}}
    conn_options=NOT_GIVEN, vad=NOT_GIVEN)
```
- **Provider-specific options go through `extra_kwargs`** (typed dicts per provider). Deepgram: `DeepgramOptions` — `filler_words` (**default True**), `interim_results` (True), `endpointing` (25 ms), `punctuate` (True), `smart_format`, `keywords`, `keyterm`, `profanity_filter`, `numerals`, `mip_opt_out`, `vad_events`, `diarize`, `dictation`, `detect_language`, `no_delay` (True), `utterance_end`, `redact`, `replace`, `search`, `tag`, `channels`, `version`, `callback`, `callback_method`, `extra` (source TypedDict). Flux: `DeepgramFluxOptions` (`eager_eot_threshold` 0.5, `eot_threshold`, `eot_timeout_ms`, `keyterm`, `mip_opt_out`, `tag`, `detect_language`).
- Usage docs: https://docs.livekit.io/agents/models/stt/deepgram.md — `inference.STT(model="deepgram/nova-3", language="en")` and `extra_kwargs={"filler_words": True}` (the model-parameters table lists `filler_words` default True for Nova models).

**STT models available** (source `STTModels` Literal): `deepgram/nova-3`, `deepgram/nova-3-medical`, `deepgram/nova-2`, `deepgram/nova-2-medical`, `deepgram/nova-2-conversationalai`, `deepgram/nova-2-phonecall`; `deepgram/flux-general[-en|-multi]`; `cartesia/ink-whisper`, `cartesia/ink-2`; `assemblyai/universal-streaming[-multilingual]`, `assemblyai/u3-rt-pro`, `assemblyai/universal-3-5-pro`; `xai/stt-1`; `speechmatics/enhanced|standard|linden-1`; `inworld/inworld-stt-1`; `google/gemini-3.5-transcribe-live`; `"auto"` (language-based server-side provider pick).

**LLM** — https://docs.livekit.io/agents/models/llm/openai.md (Inference section):

```python
inference.LLM(model="openai/gpt-5-mini", provider="openai", extra_kwargs={"reasoning_effort": "low"})
```
Params: `model` (provider-prefixed ID), `provider` (e.g. `openai`), `extra_kwargs` (temperature, top_p, max_completion_tokens, reasoning_effort, tool_choice, parallel_tool_calls, seed, stop, logprobs, top_logprobs, logit_bias, ...). LLM model table includes `openai/gpt-5.6-luna|sol|terra`, `openai/gpt-5.5`, `openai/gpt-5.4[-mini|-nano]`, `openai/gpt-5.2`, `openai/gpt-5.1`, `openai/gpt-5[-mini|-nano]`, `openai/gpt-4.1[-mini|-nano]`, `openai/chat-latest`, `openai/gpt-oss-120b` (baseten/groq), `google/gemma-4-31b-it` (quickstart default). TTS table includes `rime/coda`, `rime/mist[|v2|v3]`, `inworld/inworld-tts-2`, etc. (https://docs.livekit.io/agents/models/tts/rime/).

**String shorthand**: `AgentSession(stt="deepgram/nova-3:en", llm="openai/chat-latest", tts="rime/coda:celeste")` — verified in source (`AgentSession.__init__` string coercion -> `inference.STT.from_model_string` / `LLM.from_model_string` / `TTS.from_model_string`) and in each model docs page ("String descriptors"). The `:lang` suffix is parsed by `_parse_model_string`.

**Auth**: needs ONLY LiveKit keys — `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` (overridable via `LIVEKIT_INFERENCE_API_KEY`/`LIVEKIT_INFERENCE_API_SECRET`); no per-provider keys. Docs (every model page): "No separate provider API key is required, and usage and rate limits are managed through LiveKit Cloud."

**Pricing / free-tier credits** — **[UNVERIFIED]**: no free-credit amount stated on any page read. Pricing page referenced: https://livekit.com/pricing/inference. LiveKit Cloud sign-up includes a free project with inference included (https://docs.livekit.io/agents/start/voice-ai.md), but exact credit amounts were not found.

Inference STT word-alignment is gated per-model: `_WORD_ALIGNED_MODELS` in source includes all `deepgram/nova-*` and `deepgram/flux-*` models (=> `aligned_transcript="word"`); models outside this set get no word timings and adaptive interruption cannot use word gating.

---

### Cross-cutting gotchas for Podium
- `RoomInputOptions`/`RoomOutputOptions` are deprecated aliases; current API is `room_io.RoomOptions(audio_input=..., audio_output=..., text_output=...)` (source: `room_options=room_io.RoomOptions(...)`; deprecated `room_input_options=` kwargs on `AgentSession.start`).
- Docs plugin pages say `uv add "livekit-agents[rime]~=1.5"` but PyPI latest is 1.8.0 — install `~=1.8`.
- Session-level `metrics_collected` is deprecated in 1.8 -> `session_usage_updated` / `ChatMessage.metrics`.
- JS `RoomEvent.TranscriptionReceived` is deprecated -> `registerTextStreamHandler('lk.transcription', ...)`.
- Rime `sample_rate` default conflicts between docs (16000) and `main` source (22050) — pin it explicitly.
- NOTE FOR MAIN: this sandbox's `write` tool rejected `local://livekit-agents.md` (xd://-only); save this report verbatim to `D:/Workspace/dataforge-rime/livekit-agents.md`.