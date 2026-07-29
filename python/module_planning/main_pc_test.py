import serial
import time

# Adjust to your board's COM port
PORT = "COM7"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=2.2)

def send_and_recv(data: bytes):
    ser.write(data)
    time.sleep(0.05)
    return ser.read(len(data))

tests = [
    b"hello",
    b"1234567890",
    b"\x00\x01\x02\x03\x04",
    b"The quick brown fox",
]

for t in tests:
    r = send_and_recv(t)
    print("Sent:", t)
    print("Recv:", r)
    print()

