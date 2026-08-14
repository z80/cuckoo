import os
import sys
from pydub import AudioSegment
import numpy as np

# --- CONFIG ---
TARGET_SAMPLE_RATE = 16000

def load_audio_buffers( folder_path ):
    all_buffers = {}
    # --- PROCESS ALL .wav FILES ---
    for filename in os.listdir(folder_path):
        if filename.lower().endswith(".wav"):
            input_path = os.path.join(folder_path, filename)
            base_name = os.path.splitext(filename)[0]
            output_name = base_name.encode("ascii", errors="ignore").decode() + ".raw"
            output_path = os.path.join(folder_path, output_name)

            print("Reading:", filename)

            # --- LOAD & RESAMPLE ---
            audio = AudioSegment.from_file(input_path)
            audio = audio.set_channels(1)
            audio = audio.set_frame_rate(TARGET_SAMPLE_RATE)
            samples = np.array(audio.get_array_of_samples(), dtype=np.int16)

            # --- SCALE TO 12-BIT UNSIGNED ---
            samples_12bit = ((samples.astype(np.int32) + 32768) >> 4).astype(np.uint16)
            samples_byte_array = samples_12bit.tobytes()

            all_buffers[filename] = samples_byte_array

    print("Done loading buffers.")

    return all_buffers


