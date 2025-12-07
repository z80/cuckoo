import tkinter as tk
from tkinter import ttk, filedialog
import subprocess

class DeadzoneCalibrator:
    def __init__(self, root):
        self.root = root
        self.root.title("Deadzone Calibration (DAC units)")

        # Bound selection
        tk.Label(root, text="Select bound to calibrate:").pack()
        self.bound_var = tk.StringVar(value="X_low")
        for bound in ["X_low", "X_high", "Y_low", "Y_high"]:
            tk.Button(root, text=bound,
                      command=lambda b=bound: self.select_bound(b)).pack(side=tk.LEFT)

        # Range inputs (auto-set when bound selected)
        tk.Label(root, text="Lower DAC bound (0-4095):").pack()
        self.lower_entry = tk.Entry(root); self.lower_entry.pack()
        tk.Label(root, text="Upper DAC bound (0-4095):").pack()
        self.upper_entry = tk.Entry(root); self.upper_entry.pack()

        # Duration input
        tk.Label(root, text="Duration (seconds, 0.1-10):").pack()
        self.duration_entry = tk.Entry(root); self.duration_entry.insert(0, "3"); self.duration_entry.pack()

        # Switch delay input
        tk.Label(root, text="Switch delay before applying (seconds, 0-5):").pack()
        self.delay_entry = tk.Entry(root); self.delay_entry.insert(0, "2"); self.delay_entry.pack()

        # Run experiment button
        tk.Button(root, text="Run Experiment", command=self.run_experiment).pack()

        # Outcome buttons
        tk.Button(root, text="Yes (moved)", command=lambda: self.outcome(True)).pack(side=tk.LEFT)
        tk.Button(root, text="No (did not move)", command=lambda: self.outcome(False)).pack(side=tk.LEFT)

        # Save and Load results buttons
        tk.Button(root, text="Save Results", command=self.save_results).pack()
        tk.Button(root, text="Load Results", command=self.load_results).pack()

        # Status
        self.status = tk.Label(root, text="Idle")
        self.status.pack()

        # Progress bar
        self.progress = ttk.Progressbar(root, length=200, mode="determinate")
        self.progress.pack()

        # Log window
        tk.Label(root, text="Calibration Log:").pack()
        self.log = tk.Text(root, height=12, width=60)
        self.log.pack()

        # Binary search state
        self.low = None
        self.high = None
        self.mid = None
        self.results = {}

        # Initialize with default bound
        self.select_bound("X_low")

    def select_bound(self, bound):
        self.bound_var.set(bound)
        # Hard-coded initial ranges
        if bound == "X_low":
            self.lower_entry.delete(0, tk.END); self.lower_entry.insert(0, "2748")
            self.upper_entry.delete(0, tk.END); self.upper_entry.insert(0, "0")
        elif bound == "X_high":
            self.lower_entry.delete(0, tk.END); self.lower_entry.insert(0, "2749")
            self.upper_entry.delete(0, tk.END); self.upper_entry.insert(0, "4095")
        elif bound == "Y_low":
            self.lower_entry.delete(0, tk.END); self.lower_entry.insert(0, "2748")
            self.upper_entry.delete(0, tk.END); self.upper_entry.insert(0, "0")
        elif bound == "Y_high":
            self.lower_entry.delete(0, tk.END); self.lower_entry.insert(0, "2749")
            self.upper_entry.delete(0, tk.END); self.upper_entry.insert(0, "4095")

    def validate_inputs(self):
        try:
            low = int(self.lower_entry.get())
            high = int(self.upper_entry.get())
            duration = float(self.duration_entry.get())
            delay = float(self.delay_entry.get())
        except ValueError:
            self.status.config(text="Error: Inputs must be numbers")
            return None

        if not (0 <= low <= 4095 and 0 <= high <= 4095):
            self.status.config(text="Error: DAC bounds must be within [0, 4095]")
            return None
        if not (0.1 <= duration <= 10):
            self.status.config(text="Error: Duration must be between 0.1 and 10 seconds")
            return None
        if not (0 <= delay <= 5):
            self.status.config(text="Error: Delay must be between 0 and 5 seconds")
            return None


        return low, high, duration, delay

    def run_experiment(self):
        validated = self.validate_inputs()
        if not validated:
            return
        self.low, self.high, self.duration, self.delay = validated
        self.log.delete("1.0", tk.END)
        self.log.insert(tk.END, "Starting {} calibration...\n".format(self.bound_var.get()))
        self.next_step()

    def next_step(self):
        self.mid = (self.low + self.high) // 2
        self.status.config(text="Switch to game window...")

    def run_experiment(self):
        validated = self.validate_inputs()
        if not validated:
            return
        self.low, self.high, self.duration, self.delay = validated
        self.log.insert(tk.END, "Starting {} calibration...\n".format(self.bound_var.get()))
        self.next_step()

    def next_step(self):
        self.mid = (self.low + self.high) // 2
        self.status.config(text="Switch to game window...")
        self.log.insert(tk.END, "Preparing to test DAC {} | Span: {} -> {}\n".format(self.mid, self.low, self.high))
        self.progress["value"] = 0
        self.progress["maximum"] = int(self.delay * 10)
        self.update_progress(0)

    def update_progress(self, step):
        if step < int(self.delay * 10):
            self.progress["value"] = step
            self.root.after(100, self.update_progress, step + 1)
        else:
            bound_name = self.bound_var.get()
            axis = "x" if bound_name.startswith("X") else "y"
            cmd = "import calibration; calibration.send_pulse('{}', {}, {})".format(axis, self.duration, self.mid)
            subprocess.run(["mpremote", "connect", "COM5", "exec", cmd])
            self.status.config(text="Applied {} axis DAC {} for {}s. Did crosshair move?".format(axis, self.mid, self.duration))
            self.log.insert(tk.END, "Tested {} axis DAC {} | Span: {} -> {}\n".format(axis, self.mid, self.low, self.high))

    def outcome(self, moved):
        if self.mid is None:
            return
        if moved:
            self.high = self.mid
            self.log.insert(tk.END, "Outcome: YES -> New span {} -> {}\n".format(self.low, self.high))
        else:
            self.low = self.mid
            self.log.insert(tk.END, "Outcome: NO -> New span {} -> {}\n".format(self.low, self.high))

        # Update entry boxes to reflect new span
        self.lower_entry.delete(0, tk.END)
        self.lower_entry.insert(0, str(self.low))
        self.upper_entry.delete(0, tk.END)
        self.upper_entry.insert(0, str(self.high))

        self.mid = None

        if abs(self.high - self.low) <= 1:
            final_val = self.high
            bound_name = self.bound_var.get()
            self.status.config(text="{} bound ~= {} DAC units".format(bound_name, final_val))
            self.log.insert(tk.END, "Converged: {} ~= {} DAC units\n".format(bound_name, final_val))
            self.results[bound_name] = final_val


    def save_results(self):
        if not self.results:
            self.status.config(text="No results to save yet")
            return
        with open("calibration_results.txt", "w") as f:
            for bound, value in self.results.items():
                f.write("{}: {} DAC units\n".format(bound, value))
        self.status.config(text="Results saved to calibration_results.txt")
        self.log.insert(tk.END, "Results saved to calibration_results.txt\n")

    def load_results(self):
        try:
            filename = filedialog.askopenfilename(title="Select results file", filetypes=[("Text files", "*.txt")])
            if not filename:
                return
            loaded = {}
            with open(filename, "r") as f:
                for line in f:
                    parts = line.strip().split(":")
                    if len(parts) == 2:
                        bound = parts[0].strip()
                        val = int(parts[1].split()[0])
                        loaded[bound] = val
            self.results.update(loaded)
            self.status.config(text="Results loaded from {}".format(filename))
            self.log.insert(tk.END, "Results loaded: {}\n".format(loaded))
        except Exception as e:
            self.status.config(text="Error loading results: {}".format(e))

root = tk.Tk()
app = DeadzoneCalibrator(root)
root.mainloop()


