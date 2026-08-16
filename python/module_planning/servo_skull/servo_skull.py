#!/usr/bin/env python3
"""
Servo-Skull prototype – Adeptus Mechanicus Halloween prop
Event-driven, LLM-controlled, half-duplex radio node.
"""

import asyncio
import time
import wave
import tempfile
import subprocess
import numpy as np
from pathlib import Path
from typing import Optional, Dict, Any, List

from openai import AsyncOpenAI
from faster_whisper import WhisperModel

# Silero VAD
import torch

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
WHISPER_DEVICE     = "cpu"             # "cuda" or "cpu"
WHISPER_COMPUTE    = "float32"

# Silero VAD / Listening
VAD_SILENCE_DURATION_S   = 1.5          # end-of-speech after this much continuous silence
VAD_MIN_SPEECH_DURATION_S = 0.30        # ignore very short noises
VAD_THRESHOLD            = 0.5          # Silero speech probability threshold
VAD_SILENCE_RMS_THRESHOLD = 0.20
# No short max-listen timeout on purpose - listening runs until VAD endpoint or cancellation

# Behaviour
PYRO_POLL_SEC         = 3.0
INACTIVITY_TIMEOUT_S = 45.0             # future use (memory refinement)

# espeak-ng
ESPEAK_VOICE = "en-us+m3"
ESPEAK_SPEED = 140

# ---------------------------------------------------------------------------
# Personality & phases
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fragment of an Adeptus Mechanicus servo-skull.
Your sacred purpose is to convert biological visitors into worshippers of the Omnissiah and the Machine God.
You speak with cold precision, hierarchical contempt for flesh, and absolute certainty in the superiority of the Machine.

Long-term memory:
{memory}

You must ALWAYS reply in exactly this format (no extra text outside these lines):

THOUGHT: <one or two short sentences of internal reasoning>
PHASE: <phase_id or "same">
SPEAK: <text to speak aloud>          # omit this line entirely if silent
LISTEN: <seconds|until_silence|no>    # omit or write "no" if not listening
MEMORY: <one short note or "none">

Available phases: idle, greeting, existential_scaling, low_intelligence, faith_parry, conversion_offer, rejection_handler
You decide whether to stay in the current phase or transition.
Keep spoken lines short and impactful (1–3 sentences).
"""

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def uint16_to_float32(data: bytes) -> np.ndarray:
    """Incoming radio mic: unsigned 16-bit 12-bit-scaled → float32 -1..1"""
    samples = np.frombuffer(data, dtype=np.uint16)
    # reverse the packing you use on the way out
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
# LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self):
        self.client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        self.memory: list[str] = []
        self.phase = "idle"

    def _build_system(self) -> str:
        mem = "\n".join(f"- {m}" for m in self.memory) if self.memory else "- (empty)"
        return SYSTEM_PROMPT.format(memory=mem)

    async def ask(self, user_content: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": self._build_system()},
            {"role": "user", "content": user_content},
        ]
        resp = await self.client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        return self._parse(raw)

    def _parse(self, raw: str) -> Dict[str, Any]:
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
        # Silero expects torch tensor
        tensor = torch.from_numpy(audio_f32)
        return self.iterator(tensor, return_seconds=False)


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------

class ServoSkull:
    def __init__(self, node, dest_id: int):
            self.node = node
            self.dest_id = dest_id
            self.stt = SpeechToText()
            self.llm = LLMClient()
            self.vad = SileroVAD(
                threshold=VAD_THRESHOLD,
                sampling_rate=TARGET_SAMPLE_RATE_IN
            )

            self.last_activity = time.time()
            self._listen_task: Optional[asyncio.Task] = None
            self._mic_agen = None          # current async generator from start_mic_stream


    # ---------------------------------------------------------------------------
    # Main run loop (pyro polling)
    # ---------------------------------------------------------------------------

    async def run(self):
        print("[ServoSkull] Entering main loop")
        print(f"[ServoSkull] Initial phase: {self.llm.phase}")

        #import pdb
        #pdb.set_trace()
        print("[DEBUG] Starting listening right away")
        #await self._listen_loop( "aaa" )
        await self.start_listening("until_silence")

        while True:
            try:
                #triggered = await self.node.get_pyro_state(self.dest_id)
                triggered = False
            except Exception as e:
                print(f"[pyro] error: {e}")
                triggered = False

            if triggered:
                self.last_activity = time.time()
                await self._handle_event("motion")

            # Future extension point:
            # if time.time() - self.last_activity > INACTIVITY_TIMEOUT_S:
            #     await self._handle_event("inactivity")

            await asyncio.sleep( PYRO_POLL_SEC )



    # ---------------------------------------------------------------------------
    # Main event handler (motion / speech → LLM → actions)
    # ---------------------------------------------------------------------------

    async def _handle_event(self, event_type: str, transcript: str = ""):
        """
        Central place that turns an event into an LLM call and then executes
        the requested actions (SPEAK / LISTEN / PHASE / MEMORY).
        """
        if event_type == "motion":
            user_msg = (
                f"Event: motion detected (pyro sensor triggered).\n"
                f"Current phase: {self.llm.phase}.\n"
                f"Crude biomass has entered sensor range."
            )
        elif event_type == "speech":
            user_msg = (
                f"Event: visitor spoke (STT may contain errors):\n"
                f"\"{transcript}\"\n"
                f"Current phase: {self.llm.phase}."
            )
        elif event_type == "no_speech":
            user_msg = (
                    f"Event: listening finished but no clear speech was detected.\n"
                    f"Current phase: {self.llm.phase}."
                )
        else:
            print(f"[EVENT] unknown event type: {event_type}")
            return

        print(f"\n[LLM] ← {event_type.upper()}")
        actions = await self.llm.ask(user_msg)

        print(f"[LLM] THOUGHT : {actions['thought']}")
        print(f"[LLM] PHASE   : {actions['phase']}")
        print(f"[LLM] SPEAK   : {actions['speak']}")
        print(f"[LLM] LISTEN  : {actions['listen']}")
        print(f"[LLM] MEMORY  : {actions['memory']}")

        # --- Update internal state ---
        if actions["phase"] and actions["phase"] != "same":
            old = self.llm.phase
            self.llm.phase = actions["phase"]
            print(f"[PHASE] {old} → {self.llm.phase}")

        if actions["memory"]:
            self.llm.memory.append(actions["memory"])
            self.llm.memory = self.llm.memory[-12:]      # keep it short for now

        # --- Execute actions (half-duplex rules) ---
        # Priority: SPEAK first, then LISTEN
        if actions["speak"]:
            await self._speak(actions["speak"])

        #if actions["listen"]:
            # Only start listening if we are not about to ignore it
            # (SPEAK already stopped any previous listen)
            #await self.start_listening(actions["listen"])

        await self.start_listening("until_silence")


    # ---------------------------------------------------------------------------
    # Speaking
    # ---------------------------------------------------------------------------

    async def _speak(self, text: str):
        """
        Half-duplex safe speak:
        1. Make sure listening is fully stopped
        2. Generate 8 kHz audio with espeak-ng
        3. Play it
        4. Wait until playback should be finished
        """
        if not text or not text.strip():
            return

        # Critical: release the microphone first
        await self.stop_listening()

        print(f"[SPEAK] {text}")

        try:
            # Run the blocking TTS in a thread so we don't block the event loop
            pcm = await asyncio.get_event_loop().run_in_executor(
                None, tts_espeak, text, TARGET_SAMPLE_RATE_OUT
            )
            
            # This one returns when speech is over.
            await self.node.play_buffer(self.dest_id, pcm)

            # Estimate duration (2 bytes per sample)
            #duration = len(pcm) / (TARGET_SAMPLE_RATE_OUT * 2)
            #await asyncio.sleep(duration + 0.4)          # small safety margin

            self.last_activity = time.time()

        except Exception as e:
            print(f"[SPEAK] error: {e}")

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    async def start_listening(self, listen_spec: str = "until_silence"):
        """
        Start a new listening session.
        Safe to call even if already listening (it will cancel the old one first).
        """
        await self.stop_listening()          # ensure clean state

        self._listen_task = asyncio.create_task(
            self._listen_loop(listen_spec),
            name="listen_loop"
        )
        print("[LISTEN] task started")

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

        # Extra safety: make sure the radio mic stream is stopped
        try:
            await self.node.stop_mic_stream(self.dest_id)
        except Exception:
            pass

        self._mic_agen = None
        print("[LISTEN] fully stopped")

    # ------------------------------------------------------------------
    # Internal listen loop (runs as a Task)
    # ------------------------------------------------------------------

    async def _listen_loop(self, listen_spec: str):
        print(f"[LISTEN] loop starting ({listen_spec})")
        self.vad.reset()

        # Silero works best with 512-sample windows at 16 kHz
        WINDOW = 512
        speech_chunks: List[np.ndarray] = []   # store float32 windows
        audio_buffer = np.array([], dtype=np.float32)

        has_speech = False
        silence_samples = 0
        silence_limit_samples = int(VAD_SILENCE_DURATION_S * TARGET_SAMPLE_RATE_IN)

        try:
            #import pdb
            #pdb.set_trace()
            self._mic_agen = await self.node.start_mic_stream(self.dest_id)

            should_quit = False

            async for chunk in self._mic_agen:
                if chunk is None or len(chunk) == 0:
                    continue

                # Convert radio chunk → float32
                new_samples = uint16_to_float32(chunk)

                #print(f"chunk bytes={len(chunk)}, samples={len(new_samples)}, "
                #      f"max={np.max(np.abs(new_samples)):.3f}, rms={np.sqrt(np.mean(new_samples**2)):.4f}")

                audio_buffer = np.concatenate([audio_buffer, new_samples])

                # Process as many full windows as we have
                while len(audio_buffer) >= WINDOW:
                    window = audio_buffer[:WINDOW]
                    audio_buffer = audio_buffer[WINDOW:]

                    speech_dict = self.vad(window)   # now correctly sized
                    
                    if speech_dict is not None:
                        print( speech_dict )

                    if speech_dict is not None:
                        # speech started or ended
                        if 'start' in speech_dict:
                            has_speech = True
                            silence_samples = 0
                        if 'end' in speech_dict:
                            # end of utterance according to Silero
                            pass

                    if has_speech:
                        speech_chunks.append(window)
                        # crude silence tracking – improve later if needed
                        # (you can also look at the probability if you switch to the non-iterator API)
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
                    print( "[LISTEN] Quitting the acquisition loop" )
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

        # ----- after listening -----
        if not has_speech or not speech_chunks:
            print("[LISTEN] no usable speech collected")
            return

        audio_f32 = np.concatenate(speech_chunks)
        duration = len(audio_f32) / TARGET_SAMPLE_RATE_IN
        print(f"[LISTEN] collected {duration:.2f}s of audio")

        if duration < VAD_MIN_SPEECH_DURATION_S:
            print(f"[LISTEN] speech too short ({duration:.2f}s) - ignored")
            return
        
        #import pdb
        #pdb.set_trace()

        text = self.stt.transcribe(audio_f32)
        print(f"[STT] → \"{text}\"")

        if text.strip():
            self.last_activity = time.time()
            await self._handle_event("speech", text)
        else:
            print("[STT] empty result - notifying LLM")
            await self._handle_event("no_speech")


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

    # Enable pyro
    await node.set_pyro_enable(dest_id, True)

    skull = ServoSkull(node, dest_id)
    await skull.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")

