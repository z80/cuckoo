import numpy as np

class VectorizedMetallicVoiceChain:
    """A high-performance, NumPy-vectorized metallic DSP audio pipeline."""
    def __init__(self, sample_rate: int = 16000, carrier_freq: float = 40.0, delay_samples: int = 100, feedback_gain: float = 0.4):
        self.sample_rate = sample_rate
        self.carrier_freq = carrier_freq
        self.delay_samples = max(1, delay_samples)
        self.feedback_gain = feedback_gain
        
        # State memory for continuous block-to-block processing across the comb filter
        self._delay_line = np.zeros(self.delay_samples, dtype=np.float32)

    def process_stream(self, pcm_bytes: bytes) -> bytes:
        """Processes raw 16-bit PCM bytes through vectorized DSP routines."""
        if not pcm_bytes:
            return b""

        # Convert bytes to float32 numpy array normalized to [-1.0, 1.0]
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
        n_samples = len(audio)
        
        if n_samples == 0:
            return b""

        # --- Stage 1: Vectorized Ring Modulation ---
        # Generate time indices for the exact length of the incoming buffer
        t = np.arange(n_samples, dtype=np.float32) / self.sample_rate
        carrier = np.sin(2.0 * np.pi * self.carrier_freq * t, dtype=np.float32)
        modulated = audio * carrier

        # --- Stage 2: Vectorized Comb Filter (Cavity Resonance) ---
        # Concatenate previous state history with current input to handle block boundaries seamlessly
        extended = np.concatenate((self._delay_line, modulated))
        output = np.empty_like(modulated)
        
        # Fast recursive difference-equation calculation using array slices
        for i in range(n_samples):
            # Access delayed sample from the sliding history window
            delayed = extended[i] # points to index i in extended which corresponds to delay buffer offset
            val = modulated[i] + (delayed * self.feedback_gain)
            output[i] = val
            # Update history buffer window inside extended array
            extended[i + self.delay_samples] = val + (delayed * self.feedback_gain * 0.5)

        # Save the tail end of the delay line for the next audio chunk
        self._delay_line = extended[n_samples : n_samples + self.delay_samples]

        # --- Stage 3: Soft Clipping & Quantization ---
        np.clip(output, -1.0, 1.0, out=output)
        
        # Convert back to raw 16-bit PCM bytes
        pcm_out = (output * 32767.0).astype(np.int16)
        return pcm_out.tobytes()
