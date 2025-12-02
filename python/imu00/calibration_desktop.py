import tkinter as tk
import subprocess

class DeadzoneCalibrator:
    def __init__(self, root):
        self.root = root
        self.root.title("Deadzone Calibration (DAC units)")

        # Bound selection
        tk.Label(root, text="Select bound to calibrate:").pack()
        self.bound_var = tk.StringVar(value="X_high")
        for bound in ["X_high", "X_low", "Y_high", "Y_low"]:
            tk.Button(root, text=bound,
                      command=lambda b=bound: self.bound_var.set(b)).pack(side=tk.LEFT)

        # Search range inputs (DAC units)
        tk.Label(root, text="Lower DAC bound (0-4095):").pack()
        self.lower_entry = tk.Entry(root)
        self.lower_entry.insert(0, "1800")
        self.lower_entry.pack()

        tk.Label(root, text="Upper DAC bound (0-4095):").pack()
        self.upper_entry = tk.Entry(root)
        self.upper_entry.insert(0, "2300")
        self.upper_entry.pack()

        # Duration input
        tk.Label(root, text="Duration (seconds, 0.1-10):").pack()
        self.duration_entry = tk.Entry(root)
        self.duration_entry.insert(0, "3")
        self.duration_entry.pack()

        # Run experiment button
        tk.Button(root, text="Run Experiment", command=self.run_experiment).pack()

        # Outcome buttons
        tk.Button(root, text="Yes (moved)", command=lambda: self.outcome(True)).pack(side=tk.LEFT)
        tk.Button(root, text="No (did not move)", command=lambda: self.outcome(False)).pack(side=tk.LEFT)

        # Save results button
        tk.Button(root, text="Save Results", command=self.save_results).pack()

        # Status
        self.status = tk.Label(root, text="Idle")
        self.status.pack()

        # Log window
        tk.Label(root, text="Calibration Log:").pack()
        self.log = tk.Text(root, height=12, width=60)
        self.log.pack()

        # Binary search state
        self.low = None
        self.high = None
        self.mid = None
        self.results = {}  # store final bounds per axis

    def validate_inputs(self):
        try:
            low = int(self.lower_entry.get())
            high = int(self.upper_entry.get())
            duration = float(self.duration_entry.get())
        except ValueError:
            self.status.config(text="Error: Inputs must be numbers")
            return None

        if not (0 <= low <= 4095 and 0 <= high <= 4095):
            self.status.config(text="Error: DAC bounds must be within [0, 4095]")
            return None
        if not (0.1 <= duration <= 10):
            self.status.config(text="Error: Duration must be between 0.1 and 10 seconds")
            return None
        if low >= high:
            self.status.config(text="Error: Lower bound must be less than upper bound")
            return None

        return low, high, duration

    def run_experiment(self):
        validated = self.validate_inputs()
        if not validated:
            return
        self.low, self.high, self.duration = validated
        self.log.delete("1.0", tk.END)  # clear previous log
        self.log.insert(tk.END, "Starting {} calibration...\n".format(self.bound_var.get()))
        self.next_step()

    def next_step(self):
        self.mid = (self.low + self.high) // 2
        cmd = "import calibrate; calibrate.run({}, {})".format(self.mid, self.duration)
        subprocess.run(["mpremote", "connect", "COM5", "exec", cmd])
        self.status.config(text="Applied DAC {} for {}s. Did crosshair move?".format(self.mid, self.duration))
        self.log.insert(tk.END, "Tested DAC {} | Span: {} -> {}\n".format(self.mid, self.low, self.high))

    def outcome(self, moved):
        if self.mid is None:
            return  # ignore duplicate clicks

        if moved:
            self.high = self.mid
            self.log.insert(tk.END, "Outcome: YES -> New span {} -> {}\n".format(self.low, self.high))
        else:
            self.low = self.mid
            self.log.insert(tk.END, "Outcome: NO -> New span {} -> {}\n".format(self.low, self.high))

        self.mid = None  # reset to prevent double adjustment

        if abs(self.high - self.low) <= 1:  # convergence threshold in DAC units
            final_val = self.high
            bound_name = self.bound_var.get()
            self.status.config(text="{} bound ~= {} DAC units".format(bound_name, final_val))
            self.log.insert(tk.END, "Converged: {} ~= {} DAC units\n".format(bound_name, final_val))
            self.results[bound_name] = final_val
        else:
            self.next_step()

    def save_results(self):
        if not self.results:
            self.status.config(text="No results to save yet")
            return
        with open("calibration_results.txt", "w") as f:
            for bound, value in self.results.items():
                f.write("{}: {} DAC units\n".format(bound, value))
        self.status.config(text="Results saved to calibration_results.txt")
        self.log.insert(tk.END, "Results saved to calibration_results.txt\n")

root = tk.Tk()
app = DeadzoneCalibrator(root)
root.mainloop()


