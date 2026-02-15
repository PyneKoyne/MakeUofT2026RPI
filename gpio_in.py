import serial
import json
import threading
from queue import Queue

# Serial data queue (thread-safe)
serial_queue = Queue()

def read_serial_data():
    """Continuously read data from ESP32 and put it in the queue."""
    ser = None
    try:
        ser = serial.Serial('/dev/serial0', 115200, timeout=3)
        ser.reset_input_buffer()
        print("Serial connection established with ESP32")

        while True:
            if ser.in_waiting > 0:
                line = json.loads(ser.readline().decode('utf-8').rstrip())
                if line:
                    serial_queue.put(line)
                    print(f"[GPIO] Received: {line}")

    except Exception as e:
        print(f"Serial read error: {e}")
    finally:
        if ser is not None and ser.is_open:
            ser.close()

def start_serial_thread():
    """Start the serial reading thread as a daemon."""
    serial_thread = threading.Thread(target=read_serial_data, daemon=True)
    serial_thread.start()
    return serial_thread

# If running directly, start the thread
if __name__ == "__main__":
    start_serial_thread()
    try:
        while True:
            threading.Event().wait(1)  # Keep main thread alive
    except KeyboardInterrupt:
        print("Shutting down...")
