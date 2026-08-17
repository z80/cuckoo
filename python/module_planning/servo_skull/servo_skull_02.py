import asyncio
import os
import time
import wave
import tempfile
import subprocess
import numpy as np
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from openai import AsyncOpenAI
from faster_whisper import WhisperModel

# Silero VAD
import torch

# Jinja2 for prompt templates
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Configuration – edit these
# ---------------------------------------------------------------------------

# Hardware
SERIAL_PORT = "COM8"                    # change to your port
TARGET_SAMPLE_RATE_IN  = 16000
TARGET_SAMPLE_RATE_OUT = 8000

# LLM (text-generation-webui OpenAI-compatible endpoint)
LLM_BASE_URL = "http://127.0.0.1:5000/v1"
LLM_API_KEY  = "sk-no-key-required"
LLM_MODEL    = "gpt-3.5-turbo"          # name exposed by webui

# Whisper
WHISPER_MODEL_SIZE = "base.en"          # tiny.en / base.en / small.en
WHISPER_DEVICE     = "cpu"              # "cuda" or "cpu"
WHISPER_COMPUTE    = "float32"

# Silero VAD / Listening
VAD_SILENCE_DURATION_S    = 1.5         # end-of-speech after this much continuous silence
VAD_MIN_SPEECH_DURATION_S = 0.30        # ignore very short noises
VAD_THRESHOLD             = 0.5         # Silero speech probability threshold
VAD_SILENCE_RMS_THRESHOLD = 0.20

# Behaviour
PYRO_POLL_SEC        = 3.0
INACTIVITY_TIMEOUT_S = 15.0             # when reached, trigger memory consolidation

# espeak-ng
ESPEAK_VOICE = "en-us+Storm"
ESPEAK_SPEED = 140

# Prompt templates base path
PROMPTS_BASE_PATH = "servo_skull/prompts"

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def uint16_to_float32(data: bytes) -> np.ndarray:
    """Incoming radio mic: unsigned 16-bit 12-bit-scaled → float32 -1..1"""
    samples = np.frombuffer(data, dtype=np.uint16)
    signed = (samples.astype(np.int32) << 4) - 32768
    return signed.astype(np.float32) / 32768.0


def float32_to_uint16_12bit(audio: np.ndarray) -> bytes:
    """float32 -1..1 → 12-bit unsigned uint16 (same packing as your pydub path)"""
    samples = (audio * 32767.0).astype(np.int32)
    samples_12bit = ((samples + 32768) >> 4).astype(np.uint16)
    return samples_12bit.tobytes()


def resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Simple linear resample (good enough for prototype)"""
    if orig_sr == target_sr:
        return audio
    duration = len(audio) / orig_sr
    target_length = int(duration * target_sr)
    x_old = np.linspace(0, duration, len(audio), endpoint=False)
    x_new = np.linspace(0, duration, target_length, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


# ---------------------------------------------------------------------------
# TTS - espeak-ng
# ---------------------------------------------------------------------------

def tts_espeak(text: str, sample_rate: int = 8000) -> bytes:
    """Generate 8 kHz mono PCM via espeak-ng and return packed uint16 bytes"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name

    cmd = [
        "espeak-ng",
        "-v", ESPEAK_VOICE,
        "-s", str(ESPEAK_SPEED),
        "-w", wav_path,
        text
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    with wave.open(wav_path, "rb") as wf:
        assert wf.getnchannels() == 1
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    Path(wav_path).unlink(missing_ok=True)

    if sr != sample_rate:
        audio = resample(audio, sr, sample_rate)

    return float32_to_uint16_12bit(audio)


# ---------------------------------------------------------------------------
# STT - faster-whisper
# ---------------------------------------------------------------------------

class SpeechToText:
    def __init__(self):
        print(f"[STT] Loading faster-whisper {WHISPER_MODEL_SIZE} ...")
        self.model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE
        )
        print("[STT] Ready.")

    def transcribe(self, audio_f32: np.ndarray) -> str:
        segments, info = self.model.transcribe(
            audio_f32,
            language="en",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            beam_size=1,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

class PromptLibrary:
    def __init__(self, base_path: str = PROMPTS_BASE_PATH):
        self.env = Environment(
            loader=FileSystemLoader(base_path),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Jinja templates
        self.stage_selector = self.env.get_template("stage_selector.txt")
        self.response_generator = self.env.get_template("response_generator.txt")
        self.memory_consolidation = self.env.get_template("memory_consolidation.txt")

        # Parsed stage files
        self.stage_rules: Dict[str, str] = {}
        self.stage_bodies: Dict[str, str] = {}

        stages_path = os.path.join(base_path, "stages")

        for root, dirs, files in os.walk(stages_path):
            for filename in files:
                phase = os.path.splitext(filename)[0]
                file_path = os.path.join(root, filename)

                switch, body = self._load_stage_file(file_path)
                self.stage_rules[phase] = switch
                self.stage_bodies[phase] = body

                print(f"[PROMPTS] Loaded stage '{phase}'")

    def _load_stage_file(self, path: str) -> Tuple[str, str]:
        text = Path(path).read_text(encoding="utf-8")

        if "STAGE_DESCRIPTION:" not in text:
            raise ValueError(f"Stage file missing STAGE_DESCRIPTION: {path}")

        switch_part, body_part = text.split("STAGE_DESCRIPTION:", 1)

        switch = switch_part.replace("SWITCH_CRITERIA:", "").strip()
        body = body_part.strip()

        return switch, body


# ---------------------------------------------------------------------------
# LLM client (two-stage: phase selection + response generation)
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, prompts: PromptLibrary):
        self.client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        self.prompts = prompts

        self.memory: List[str] = []
        self.phase: str = "idle"
        self.dialog_history: List[str] = []

    def _build_common_context(self) -> Dict[str, Any]:
        return {
            "memory": self.memory,
            "history": self.dialog_history,
            "phase": self.phase,
        }

    async def ask_stage(
        self,
        event_type: str,
        transcript: Optional[str],
        pyro_present: bool,
        vad_triggered: bool,
    ) -> str:
        """
        LLM call #1: decide next conversation phase.
        No speech generation here, just PHASE.
        """
        ctx = self._build_common_context()
        ctx.update({
            "event": event_type,
            "input": transcript or "",
            "pyro": "present" if pyro_present else "absent",
            "vad": "triggered" if vad_triggered else "none",
        })

        system_prompt = self.prompts.stage_selector.render(
            **ctx,
            stage_rules=self.prompts.stage_rules
        )

        print("\n[DEBUG] Stage selector prompt:\n", system_prompt)

        user_msg = transcript or "(no speech)"

        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=80,
        )
        raw = resp.choices[0].message.content.strip()
        next_phase = self._parse_phase_only(raw)
        return next_phase or self.phase

    async def ask_response(
        self,
        event_type: str,
        transcript: Optional[str],
        pyro_present: bool,
        vad_triggered: bool,
    ) -> Dict[str, Any]:
        """
        LLM call #2: generate SPEAK / LISTEN / MEMORY given current phase.
        Uses phase-specific system prompt if available.
        """
        ctx = self._build_common_context()
        ctx.update({
            "event": event_type,
            "input": transcript or "",
            "pyro": "present" if pyro_present else "absent",
            "vad": "triggered" if vad_triggered else "none",
        })

        # Choose phase-specific system prompt if available, else generic response_generator
        stage_body = self.prompts.stage_bodies.get(self.phase)
        system_prompt = self.prompts.response_generator.render(
            **ctx,
            stage_body=stage_body
        )

        print("\n[DEBUG] Response generator prompt:\n", system_prompt)

        user_msg = transcript or "(no speech)"

        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        return self._parse_actions(raw)

    async def ask_memory_consolidation(self) -> Optional[str]:
        """
        LLM call for inactivity-triggered memory consolidation.
        """
        ctx = self._build_common_context()
        system_prompt = self.prompts.memory_consolidation.render(**ctx)

        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Convert recent dialog into long-term memory facts."},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()

        print("\n[DEBUG] memory concolidation raw:\n", raw)
        # You can define a specific output format; here we just return the whole text.
        raw = self._parse_actions(raw)
        raw = raw.get("memory", None)

        print("\n[DEBUG] new memory:\n", raw)
        return raw

    def _parse_phase_only(self, raw: str) -> Optional[str]:
        phase = None
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("PHASE:"):
                phase = line[6:].strip().lower()
        return phase

    def _parse_actions(self, raw: str) -> Dict[str, Any]:
        result = {
            "thought": "",
            "phase": "same",
            "speak": None,
            "listen": None,
            "memory": None,
            "raw": raw,
        }
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("THOUGHT:"):
                result["thought"] = line[8:].strip()
            elif line.upper().startswith("PHASE:"):
                result["phase"] = line[6:].strip().lower()
            elif line.upper().startswith("SPEAK:"):
                result["speak"] = line[6:].strip()
            elif line.upper().startswith("LISTEN:"):
                val = line[7:].strip().lower()
                result["listen"] = None if val in ("", "no", "none") else val
            elif line.upper().startswith("MEMORY:"):
                val = line[7:].strip()
                result["memory"] = None if val.lower() in ("", "none") else val
        return result


# ---------------------------------------------------------------------------
# Silero VAD helper
# ---------------------------------------------------------------------------

class SileroVAD:
    def __init__(self, threshold: float = 0.5, sampling_rate: int = 16000):
        self.threshold = threshold
        self.sampling_rate = sampling_rate
        print("[VAD] Loading Silero VAD...")
        self.model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=False
        )
        self.model.eval()
        (self.get_speech_timestamps,
         self.save_audio,
         self.read_audio,
         self.VADIterator,
         self.collect_chunks) = utils
        self.reset()
        print("[VAD] Ready.")

    def reset(self):
        self.iterator = self.VADIterator(self.model, threshold=self.threshold,
                                         sampling_rate=self.sampling_rate)

    def __call__(self, audio_f32: np.ndarray) -> dict:
        """
        Feed a chunk (float32, mono, 16 kHz).
        Returns the latest decision from the iterator.
        """
        tensor = torch.from_numpy(audio_f32)
        return self.iterator(tensor, return_seconds=False)


# ---------------------------------------------------------------------------
# Main controller – ServoSkull with behavioral FSM
# ---------------------------------------------------------------------------

class ServoSkull:
    """
    Behavioral FSM states (real-world behavior):
      - idle
      - greeting
      - listening
      - processing
      - responding
      - memory_consolidation

    Conversation phases (LLM internal):
      - idle, greeting, existential_scaling, low_intelligence,
        faith_parry, conversion_offer, rejection_handler
    """
    def __init__(self, node, dest_id: int):
        self.node = node
        self.dest_id = dest_id

        self.stt = SpeechToText()
        self.prompts = PromptLibrary()
        self.llm = LLMClient(self.prompts)
        self.vad = SileroVAD(
            threshold=VAD_THRESHOLD,
            sampling_rate=TARGET_SAMPLE_RATE_IN
        )

        self.last_activity = time.time()
        self.behavior_state: str = "idle"

        self._listen_task: Optional[asyncio.Task] = None
        self._mic_agen = None

        # Event queue: (event_type, payload_dict)
        self.event_queue: asyncio.Queue[Tuple[str, Dict[str, Any]]] = asyncio.Queue()

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    async def run(self):
        print("[ServoSkull] Entering main loop")
        print(f"[ServoSkull] Initial conversation phase: {self.llm.phase}")
        print(f"[ServoSkull] Initial behavior state: {self.behavior_state}")

        # Start FSM loop
        asyncio.create_task(self._fsm_loop(), name="fsm_loop")

        # Start pyro polling loop
        asyncio.create_task(self._pyro_loop(), name="pyro_loop")

        # Start initial idle listening if you want, or just idle
        # For now, stay in idle and let pyro drive greeting.

        # Keep the main task alive
        while True:
            await asyncio.sleep(1.0)

    # -----------------------------------------------------------------------
    # Pyro polling loop – emits events only
    # -----------------------------------------------------------------------

    async def _pyro_loop(self):
        while True:
            try:
                triggered = await self.node.get_pyro_state(self.dest_id)
            except Exception as e:
                print(f"[pyro] error: {e}")
                triggered = False

            if triggered:
                await self.event_queue.put(("motion", {"pyro_present": True}))
            else:
                await self.event_queue.put(("motion", {"pyro_present": False}))

            await asyncio.sleep(PYRO_POLL_SEC)

    # -----------------------------------------------------------------------
    # FSM loop – single place where behavior decisions are made
    # -----------------------------------------------------------------------

    async def _fsm_loop(self):
        while True:
            event_type, payload = await self.event_queue.get()
            pyro_present = payload.get("pyro_present", False)
            transcript = payload.get("transcript")
            vad_triggered = payload.get("vad_triggered", False)

            now = time.time()
            inactivity = now - self.last_activity

            # Inactivity handling (global)
            if inactivity > INACTIVITY_TIMEOUT_S and self.behavior_state != "memory_consolidation":
                print("[FSM] Inactivity timeout reached – entering memory_consolidation")
                self.behavior_state = "memory_consolidation"
                await self._do_memory_consolidation()
                self.behavior_state = "idle"
                continue

            print(f"[FSM] state={self.behavior_state} event={event_type} pyro={pyro_present} vad={vad_triggered}")

            if self.behavior_state == "idle":
                await self._fsm_idle(event_type, pyro_present)

            elif self.behavior_state == "greeting":
                await self._fsm_greeting(event_type, pyro_present)

            elif self.behavior_state == "listening":
                await self._fsm_listening(event_type, pyro_present, vad_triggered, transcript)

            elif self.behavior_state == "processing":
                await self._fsm_processing(event_type, pyro_present, vad_triggered, transcript)

            elif self.behavior_state == "responding":
                await self._fsm_responding(event_type, pyro_present)

            elif self.behavior_state == "memory_consolidation":
                # handled above; here we just ignore events
                pass

    # -----------------------------------------------------------------------
    # FSM state handlers
    # -----------------------------------------------------------------------

    async def _fsm_idle(self, event_type: str, pyro_present: bool):
        if event_type == "motion" and pyro_present:
            print("[FSM] idle → greeting (visitor detected)")
            self.behavior_state = "greeting"
            await self._handle_visitor_arrival(pyro_present=True)

    async def _fsm_greeting(self, event_type: str, pyro_present: bool):
        # Greeting is handled by _handle_visitor_arrival; after speaking we go to listening.
        # Here we mostly ignore events until we transition.
        pass

    async def _fsm_listening(
        self,
        event_type: str,
        pyro_present: bool,
        vad_triggered: bool,
        transcript: Optional[str],
    ):
        if event_type == "speech":
            print("[FSM] listening → processing (speech captured)")
            self.behavior_state = "processing"
            await self._handle_speech(transcript, pyro_present, vad_triggered)

        elif event_type == "no_speech":
            # No VAD, listening ended
            if pyro_present:
                print("[FSM] listening: no_speech but pyro present → treat as unclear speech")
                self.behavior_state = "processing"
                await self._handle_unclear_speech(pyro_present)
            else:
                print("[FSM] listening: no_speech and no pyro → back to idle")
                self.behavior_state = "idle"

        elif event_type == "motion":
            # pyro updates while listening; you can use this to detect visitor leaving
            if not pyro_present:
                print("[FSM] listening: pyro absent → may go idle soon")
                # For now, just note; you could add a grace period.

    async def _fsm_processing(
        self,
        event_type: str,
        pyro_present: bool,
        vad_triggered: bool,
        transcript: Optional[str],
    ):
        # Processing is handled by _handle_speech / _handle_unclear_speech.
        # After LLM response, we go to responding.
        pass

    async def _fsm_responding(self, event_type: str, pyro_present: bool):
        if event_type == "tts_done":
            # After speaking, decide whether to listen again or go idle
            # This decision is encoded in last LLM actions (listen spec).
            # For simplicity, we always start listening if visitor is present.
            if pyro_present:
                print("[FSM] responding → listening (visitor still present)")
                self.behavior_state = "listening"
                await self.start_listening("until_silence")
            else:
                print("[FSM] responding → idle (visitor gone)")
                self.behavior_state = "idle"

    # -----------------------------------------------------------------------
    # High-level handlers that call LLM (two-stage) and perform actions
    # -----------------------------------------------------------------------

    async def _handle_visitor_arrival(self, pyro_present: bool):
        """
        Visitor detected by pyro; decide conversation phase, then greet.
        """
        event_type = "motion"
        transcript = None
        vad_triggered = False

        # Stage selection
        next_phase = await self.llm.ask_stage(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        print(f"[LLM] Stage selected: {next_phase}")
        self.llm.phase = next_phase

        # Response generation
        actions = await self.llm.ask_response(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        await self._apply_llm_actions(actions)

        # After greeting, go to responding; tts_done will move us to listening
        self.behavior_state = "responding"

    async def _handle_speech(
        self,
        transcript: Optional[str],
        pyro_present: bool,
        vad_triggered: bool,
    ):
        event_type = "speech"
        self.llm.dialog_history.append(f"visitor: {transcript or ''}")

        # Stage selection
        next_phase = await self.llm.ask_stage(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        print(f"[LLM] Stage selected: {next_phase}")
        self.llm.phase = next_phase

        # Response generation
        actions = await self.llm.ask_response(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        await self._apply_llm_actions(actions)

        self.behavior_state = "responding"

    async def _handle_unclear_speech(self, pyro_present: bool):
        """
        VAD triggered but Whisper produced empty or unusable text.
        Treat as 'visitor present but unclear speech'.
        """
        event_type = "no_speech"
        transcript = None
        vad_triggered = True

        # Stage selection
        next_phase = await self.llm.ask_stage(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        print(f"[LLM] Stage selected (unclear speech): {next_phase}")
        self.llm.phase = next_phase

        # Response generation
        actions = await self.llm.ask_response(
            event_type=event_type,
            transcript=transcript,
            pyro_present=pyro_present,
            vad_triggered=vad_triggered,
        )
        await self._apply_llm_actions(actions)

        self.behavior_state = "responding"

    async def _do_memory_consolidation(self):
        """
        Inactivity-triggered memory consolidation.
        """
        print("[MEMORY] Consolidating dialog into long-term memory via LLM...")
        summary = await self.llm.ask_memory_consolidation()
        if summary:
            self.llm.memory.append(summary)
            self.llm.memory = self.llm.memory[-12:]
            print("[MEMORY] Added consolidation summary.")
        else:
            print("[MEMORY] No summary produced.")
        self.last_activity = time.time()

    async def _apply_llm_actions(self, actions: Dict[str, Any]):
        print(f"[LLM] THOUGHT : {actions['thought']}")
        print(f"[LLM] PHASE   : {actions['phase']}")
        print(f"[LLM] SPEAK   : {actions['speak']}")
        print(f"[LLM] LISTEN  : {actions['listen']}")
        print(f"[LLM] MEMORY  : {actions['memory']}")

        # Update conversation phase
        if actions["phase"] and actions["phase"] != "same":
            old = self.llm.phase
            self.llm.phase = actions["phase"]
            print(f"[PHASE] {old} → {self.llm.phase}")

        # Update long-term memory
        if actions["memory"]:
            self.llm.memory.append(actions["memory"])
            self.llm.memory = self.llm.memory[-12:]

        # Execute SPEAK first
        if actions["speak"]:
            await self._speak(actions["speak"])

        # Decide listening based on LLM output
        listen_spec = actions["listen"]
        if listen_spec:
            await self.start_listening(listen_spec)

    # -----------------------------------------------------------------------
    # Speaking
    # -----------------------------------------------------------------------

    async def _speak(self, text: str):
        """
        Half-duplex safe speak:
        1. Make sure listening is fully stopped
        2. Generate 8 kHz audio with espeak-ng
        3. Play it
        4. Emit tts_done event
        """
        if not text or not text.strip():
            return

        await self.stop_listening()

        print(f"[SPEAK] {text}")

        try:
            pcm = await asyncio.get_event_loop().run_in_executor(
                None, tts_espeak, text, TARGET_SAMPLE_RATE_OUT
            )

            await self.node.play_buffer(self.dest_id, pcm)

            self.last_activity = time.time()

            # Notify FSM that TTS is done
            await self.event_queue.put(("tts_done", {"pyro_present": True}))

        except Exception as e:
            print(f"[SPEAK] error: {e}")

    # -----------------------------------------------------------------------
    # Public control methods – listening
    # -----------------------------------------------------------------------

    async def start_listening(self, listen_spec: str = "until_silence"):
        """
        Start a new listening session.
        Safe to call even if already listening (it will cancel the old one first).
        """
        await self.stop_listening()

        self._listen_task = asyncio.create_task(
            self._listen_loop(listen_spec),
            name="listen_loop"
        )
        print(f"[LISTEN] task started ({listen_spec})")

    async def stop_listening(self):
        """
        Cancel any running listen task and make sure the microphone stream is stopped.
        Blocks until the mic is fully released.
        """
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[LISTEN] stop error: {e}")

        self._listen_task = None

        try:
            await self.node.stop_mic_stream(self.dest_id)
        except Exception:
            pass

        self._mic_agen = None
        print("[LISTEN] fully stopped")

    # -----------------------------------------------------------------------
    # Internal listen loop – emits events only
    # -----------------------------------------------------------------------

    async def _listen_loop(self, listen_spec: str):
        print(f"[LISTEN] loop starting ({listen_spec})")
        self.vad.reset()

        WINDOW = 512
        speech_chunks: List[np.ndarray] = []
        audio_buffer = np.array([], dtype=np.float32)

        has_speech = False
        silence_samples = 0
        silence_limit_samples = int(VAD_SILENCE_DURATION_S * TARGET_SAMPLE_RATE_IN)

        try:
            self._mic_agen = await self.node.start_mic_stream(self.dest_id)
            should_quit = False

            async for chunk in self._mic_agen:
                if chunk is None or len(chunk) == 0:
                    continue

                new_samples = uint16_to_float32(chunk)
                audio_buffer = np.concatenate([audio_buffer, new_samples])

                while len(audio_buffer) >= WINDOW:
                    window = audio_buffer[:WINDOW]
                    audio_buffer = audio_buffer[WINDOW:]

                    speech_dict = self.vad(window)

                    if speech_dict is not None:
                        if 'start' in speech_dict:
                            has_speech = True
                            silence_samples = 0
                        if 'end' in speech_dict:
                            # end of utterance according to Silero
                            pass

                    if has_speech:
                        speech_chunks.append(window)
                        rms = np.sqrt(np.mean(window**2))
                        if rms < VAD_SILENCE_RMS_THRESHOLD:
                            silence_samples += WINDOW
                        else:
                            silence_samples = 0

                    if has_speech and silence_samples >= silence_limit_samples:
                        print("[LISTEN] VAD end-of-speech detected")
                        should_quit = True
                        break

                if should_quit:
                    print("[LISTEN] Quitting the acquisition loop")
                    break

        except asyncio.CancelledError:
            print("[LISTEN] cancelled")
            raise
        except Exception as e:
            print(f"[LISTEN] error: {e}")
        finally:
            try:
                await self.node.stop_mic_stream(self.dest_id)
            except Exception:
                pass
            self._mic_agen = None

        # After listening
        if not has_speech or not speech_chunks:
            print("[LISTEN] no usable speech collected")
            # No VAD; we need pyro info to decide what to do.
            await self.event_queue.put(
                ("no_speech", {"pyro_present": await self._get_pyro_safe(), "vad_triggered": False})
            )
            return

        audio_f32 = np.concatenate(speech_chunks)
        duration = len(audio_f32) / TARGET_SAMPLE_RATE_IN
        print(f"[LISTEN] collected {duration:.2f}s of audio")

        if duration < VAD_MIN_SPEECH_DURATION_S:
            print(f"[LISTEN] speech too short ({duration:.2f}s) - ignored")
            await self.event_queue.put(
                ("no_speech", {"pyro_present": await self._get_pyro_safe(), "vad_triggered": True})
            )
            return

        text = self.stt.transcribe(audio_f32)
        print(f"[STT] → \"{text}\"")

        self.last_activity = time.time()

        if text.strip():
            await self.event_queue.put(
                ("speech", {
                    "transcript": text,
                    "pyro_present": await self._get_pyro_safe(),
                    "vad_triggered": True
                })
            )
        else:
            print("[STT] empty result - notifying FSM as unclear speech")
            await self.event_queue.put(
                ("no_speech", {"pyro_present": await self._get_pyro_safe(), "vad_triggered": True})
            )

    async def _get_pyro_safe(self) -> bool:
        try:
            return await self.node.get_pyro_state(self.dest_id)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Entry point (re-uses your existing discovery helpers)
# ---------------------------------------------------------------------------

async def find_target(node):
    quantity = await node.get_nodes_qty()
    for index in range(quantity):
        info = await node.get_node_info(index)
        node_id = info.get("id") if info else None
        if node_id is not None and node_id != node.node_id:
            return node_id
    return None


async def main():
    from pc_hardware_node import PCHardwareNode   # your module

    port = SERIAL_PORT
    node = await PCHardwareNode.create(port=port)

    print("Waiting for NRF registration...")
    while await node.get_node_id() is None:
        await asyncio.sleep(1)
    print(f"PC node ID: {node.node_id}")

    dest_id = await find_target(node)
    if dest_id is None:
        print("No remote node found")
        return
    print(f"Remote node: {dest_id}")

    await node.set_pyro_enable(dest_id, True)

    skull = ServoSkull(node, dest_id)
    await skull.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")



