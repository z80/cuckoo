import os
import sys
from pydub import AudioSegment
import numpy as np

# --- CONFIG ---
target_sample_rate = 16000

# --- GET FOLDER PATH ---
if len(sys.argv) < 2:
    print("Usage: python convert.py <folder_path>")
    sys.exit(1)

folder_path = sys.argv[1]

# --- PROCESS ALL .wav FILES ---
for filename in os.listdir(folder_path):
    if filename.lower().endswith(".wav"):
        input_path = os.path.join(folder_path, filename)
        base_name = os.path.splitext(filename)[0]
        output_name = base_name.encode("ascii", errors="ignore").decode() + ".raw"
        output_path = os.path.join(folder_path, output_name)

        print("Converting:", filename, "→", output_name)

        # --- LOAD & RESAMPLE ---
        audio = AudioSegment.from_file(input_path)
        audio = audio.set_channels(1)
        audio = audio.set_frame_rate(target_sample_rate)
        samples = np.array(audio.get_array_of_samples(), dtype=np.int16)

        # --- SCALE TO 12-BIT UNSIGNED ---
        samples_12bit = ((samples.astype(np.int32) + 32768) >> 4).astype(np.uint16)

        # --- SAVE RAW ---
        with open(output_path, "wb") as f:
            f.write(samples_12bit.tobytes())

print("Done.")

