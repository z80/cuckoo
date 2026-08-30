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
WHISPER_DEVICE     = "cuda"              # "cuda" or "cpu"
WHISPER_COMPUTE    = "float32"

# Silero VAD / Listening
VAD_SILENCE_DURATION_S    = 1.5         # end-of-speech after this much continuous silence
VAD_MIN_SPEECH_DURATION_S = 0.30        # ignore very short noises
VAD_THRESHOLD             = 0.5         # Silero speech probability threshold
VAD_SILENCE_RMS_THRESHOLD = 0.25
LISTEN_SESSION_TIMEOUT_S  = 10.0         # Max time to listen before returning control to FSM

# Behaviour
PYRO_POLL_SEC        = 3.0
INACTIVITY_TIMEOUT_S = 60.0             # when reached, trigger memory consolidation

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
    """float32 -1..1 → 12-bit unsigned uint16"""
    samples = (audio * 32767.0).astype(np.int32)
    samples_12bit = ((samples + 32768) >> 4).astype(np.uint16)
    return samples_12bit.tobytes()


def resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Simple linear resample"""
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

        self.system_prompt  = self.env.get_template("system_prompt.txt")
        self.stage_selector = self.env.get_template("stage_selector.txt")
        self.response_generator = self.env.get_template("response_generator.txt")
        self.memory_consolidation = self.env.get_template("memory_consolidation.txt")

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
# LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, prompts: PromptLibrary):
        self.client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        self.prompts = prompts
        self.memory: List[str] = []
        self.phase: str = "idle"
        self.dialog_history: List[str] = []

    def _build_common_context(self) -> Dict[str, Any]:
        return {"memory": self.memory, "history": self.dialog_history, "phase": self.phase}

    async def ask_stage(self, event_type: str, transcript: Optional[str], pyro_present: bool) -> str:
        ctx = self._build_common_context()
        ctx.update({"input": transcript or "", "pir": "True" if pyro_present else "False", "phase": self.phase})

        system_prompt = self.prompts.system_prompt.render(**ctx, stage_rules=self.prompts.stage_rules)
        user_prompt = self.prompts.stage_selector.render(**ctx, stage_rules=self.prompts.stage_rules)

        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.3,
            max_tokens=512,
        )
        raw = resp.choices[0].message.content.strip()
        next_phase = self._parse_phase_only(raw)
        return next_phase or self.phase

    async def ask_response(self, event_type: str, transcript: Optional[str], pyro_present: bool) -> Dict[str, Any]:
        ctx = self._build_common_context()
        ctx.update({"input": transcript or "", "pir": "True" if pyro_present else "False", "phase": self.phase})

        system_prompt = self.prompts.system_prompt.render(**ctx, stage_rules=self.prompts.stage_rules)
        stage_body = self.prompts.stage_bodies.get(self.phase)
        user_prompt = self.prompts.response_generator.render(**ctx, stage_body=stage_body)

        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.7,
            max_tokens=512,
        )
        raw = resp.choices[0].message.content.strip()
        return self._parse_actions(raw)

    async def ask_memory_consolidation(self) -> Optional[str]:
        ctx = self._build_common_context()
        system_prompt = self.prompts.memory_consolidation.render(**ctx)
        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": "Convert recent dialog into long-term memory facts."}],
            temperature=0.3,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        parsed = self._parse_actions(raw)
        return parsed.get("memory")

    def _parse_phase_only(self, raw: str) -> Optional[str]:
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("PHASE:"):
                return line[6:].strip().lower()
        return None

    def _parse_actions(self, raw: str) -> Dict[str, Any]:
        result = {"thought": "", "phase": "same", "speak": None, "listen": None, "memory": None, "raw": raw}
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("THOUGHT:"): result["thought"] = line[8:].strip()
            elif line.upper().startswith("PHASE:"): result["phase"] = line[6:].strip().lower()
            elif line.upper().startswith("SPEAK:"): result["speak"] = line[6:].strip()
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
        self.model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad', model='silero_vad', force_reload=False, onnx=False)
        self.model.eval()
        (self.get_speech_timestamps, self.save_audio, self.read_audio, self.VADIterator, self.collect_chunks) = utils
        self.reset()
        print("[VAD] Ready.")

    def reset(self):
        self.iterator = self.VADIterator(self.model, threshold=self.threshold, sampling_rate=self.sampling_rate)

    def __call__(self, audio_f32: np.ndarray) -> dict:
        tensor = torch.from_numpy(audio_f32)
        return self.iterator(tensor, return_seconds=False)


# ---------------------------------------------------------------------------
# Main controller – ServoSkull
# ---------------------------------------------------------------------------

class ServoSkull:
    def __init__(self, node, dest_id):
        self.node = node
        self.dest_id = dest_id
        self.stt = SpeechToText()
        self.prompts = PromptLibrary()
        self.llm = LLMClient(self.prompts)
        self.vad = SileroVAD(threshold=VAD_THRESHOLD, sampling_rate=TARGET_SAMPLE_RATE_IN)

        self.state = "idle"
        self.last_activity = time.time()
        self.dialog_history = []

    async def run(self):
        while True:
            if self.state == "idle":
                await self._state_idle()
            elif self.state == "greeting":
                await self._state_greeting()
            elif self.state == "listening":
                await self._state_listening()
            else:
                self.state = "idle"

    async def _state_idle(self):
        await asyncio.sleep(PYRO_POLL_SEC)
        try:
            pyro = await self.node.get_pyro_state(self.dest_id)
        except:
            pyro = False
        if pyro:
            self.state = "greeting"

    async def _state_greeting(self):
        pyro = True 
        phase = await self.llm.ask_stage("motion", None, pyro)
        self.llm.phase = phase
        actions = await self.llm.ask_response("motion", None, pyro)
        text = actions.get("speak", "Greetings, traveler.")
        await self._speak(text)
        self.state = "listening"

    async def _state_listening(self):
        """
        New Logic: Decoupled activity timer from LLM response generation.
        """
        # 1. Listen for a short burst
        transcript, pyro_present, interaction_active = await self._listen_session()

        # 2. Activity Timer Reset (Decoupled from speaking)
        # Only reset the clock if there is actual physical or auditory evidence of presence
        if transcript or pyro_present:
            self.last_activity = time.time()
            print("[STATE] Activity detected - resetting inactivity timer.")
            print(f"[STATE] PIR: {pyro_present}")
            print(f"[STATE] transcript: {transcript}")


        # 3. Unified LLM Trigger
        # We always trigger the LLM regardless of whether we found speech/PIR or total silence
        event_type = "speech" if transcript else "no_speech"

        # Update internal phase based on current context
        phase = await self.llm.ask_stage(event_type, transcript, pyro_present)
        self.llm.phase = phase

        # Generate and speak response
        actions = await self.llm.ask_response(event_type, transcript, pyro_present)
        response_text = actions.get("speak")
        if response_text:
            await self._speak(response_text)

        # 4. Idle Check
        # If no activity has been seen for the timeout duration, go to idle
        if time.time() - self.last_activity > INACTIVITY_TIMEOUT_S:
            print("[STATE] Inactivity timeout reached. Consolidating memory...")
            await self._memory_consolidation_if_needed()
            self.state = "idle"
        else:
            self.state = "listening"

    async def _listen_session(self):
        """
        Returns: (transcript, pyro_present, interaction_active)
        Now includes a LISTEN_SESSION_TIMEOUT_S to prevent blocking the FSM.
        """
        print("[LISTEN] session start")
        self.vad.reset()
        start_time = time.time()

        WINDOW = 512
        speech_chunks: List[np.ndarray] = []
        audio_buffer = np.array([], dtype=np.float32)
        has_speech = False
        silence_samples = 0
        silence_limit_samples = int(VAD_SILENCE_DURATION_S * TARGET_SAMPLE_RATE_IN)
        interaction_active = False

        try:
            self._mic_agen = await self.node.start_mic_stream(self.dest_id)
            should_quit = False

            async for chunk in self._mic_agen:
                # Heartbeat Timeout: Don't block forever if it's silent
                if time.time() - start_time > LISTEN_SESSION_TIMEOUT_S:
                    print("[LISTEN] Session timed out (no speech end detected)")
                    break

                if chunk is None or len(chunk) == 0:
                    continue

                new_samples = uint16_to_float32(chunk)
                audio_buffer = np.concatenate([audio_buffer, new_samples])

                while len(audio_buffer) >= WINDOW:
                    window = audio_buffer[:WINDOW]
                    audio_buffer = audio_buffer[WINDOW:]
                    speech_dict = self.vad(window)

                    if speech_dict is not None:
                        interaction_active = True
                        if 'start' in speech_dict:
                            has_speech = True
                            silence_samples = 0

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
                if should_quit: break

        except Exception as e:
            print(f"[LISTEN] error: {e}")
        finally:
            try:
                await self.node.stop_mic_stream(self.dest_id)
            except: pass
            self._mic_agen = None

        try:
            pyro_present = await self.node.get_pyro_state(self.dest_id)
        except:
            pyro_present = False

        if not has_speech or not speech_chunks:
            return None, pyro_present, interaction_active or pyro_present

        audio_f32 = np.concatenate(speech_chunks)
        if len(audio_f32) / TARGET_SAMPLE_RATE_IN < VAD_MIN_SPEECH_DURATION_S:
            return None, pyro_present, interaction_active or pyro_present

        text = self.stt.transcribe(audio_f32).strip()
        return text if text else None, pyro_present, True


    async def _speak(self, text):
        if not text: return
        pcm = await asyncio.get_event_loop().run_in_executor(None, tts_espeak, text, TARGET_SAMPLE_RATE_OUT)
        await self.node.play_buffer(self.dest_id, pcm)

    async def _memory_consolidation_if_needed(self):
        if not self.dialog_history: return
        summary = await self.llm.ask_memory_consolidation()
        if summary:
            self.llm.memory.append(summary)
            self.llm.memory = self.llm.memory[-12:]
            self.dialog_history.clear()


# ---------------------------------------------------------------------------
# Entry point
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
    from pc_hardware_node import PCHardwareNode
    port = SERIAL_PORT
    node = await PCHardwareNode.create(port=port)
    while await node.get_node_id() is None:
        await asyncio.sleep(1)
    dest_id = await find_target(node)
    if dest_id is None: return
    await node.set_pyro_enable(dest_id, True)
    skull = ServoSkull(node, dest_id)
    await skull.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")



