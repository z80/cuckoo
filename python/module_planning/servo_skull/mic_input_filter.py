import numpy as np

class MicInputProcessor:
    """Cleans raw microphone input by removing DC bias, low rumble, and background noise."""
    def __init__(self, sample_rate: int = 16000, noise_threshold: float = 0.03):
        self.sample_rate = sample_rate
        self.noise_threshold = noise_threshold  # Amplitude cutoff for the noise gate
        
        # State memory for a simple 1-pole high-pass filter to kill sub-audible DC/wind rumble (~80 Hz)
        self.hp_alpha = 0.85
        self.last_input = 0.0
        self.last_filtered = 0.0

    def clean_stream(self, pcm_bytes: bytes) -> bytes:
        """Processes raw microphone PCM bytes: removes rumble, applies a noise gate."""
        if not pcm_bytes:
            return b""

        # Convert bytes to float32 numpy array normalized to [-1.0, 1.0]
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
        
        if len(audio) == 0:
            return b""

        # --- Stage 1: High-Pass Filter (Removes low-end wind and traffic thrum) ---
        # y[i] = alpha * (y[i-1] + x[i] - x[i-1]) vectorized equivalent or fast loop
        filtered = np.empty_like(audio)
        last_in = self.last_input
        last_filt = self.last_filtered
        
        for i in range(len(audio)):
            x = audio[i]
            # 1-pole high pass formula
            y = self.hp_alpha * (last_filt + x - last_in)
            filtered[i] = y
            last_in = x
            last_filt = y
            
        self.last_input = last_in
        self.last_filtered = last_filt

        # --- Stage 2: Soft Noise Gate (Zeroes out background highway drone during pauses) ---
        # Compute rolling short-term energy or absolute magnitude envelope
        # If signal is below the threshold, attenuate it heavily (soft gating)
        mask = np.abs(filtered) > self.noise_threshold
        gated = filtered * mask  # Hard cut or smooth mute below threshold

        # Optional: scale up the active speech to give the STT/funnel a clean signal
        gated *= 1.5 

        # Clip and convert back to 16-bit PCM bytes
        np.clip(gated, -1.0, 1.0, out=gated)
        pcm_out = (gated * 32767.0).astype(np.int16)
        return pcm_out.tobytes()
