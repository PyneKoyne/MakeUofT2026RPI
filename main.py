import socketio
import time
import requests
import json
import threading
import re
from datetime import datetime
from queue import Empty

# Import the serial queue from gpio_in
from gpio_in import serial_queue, start_serial_thread

# Create a Socket.IO client
sio = socketio.Client()

# Server URL
SERVER_URL = "https://api.pynekoyne.com"
SOCKET_PATH = "/"

# Shared data state (thread-safe with lock)
data_lock = threading.Lock()
current_sensor_data = {
    "bpm": 0,
    "value": 0,
    "last_updated": None
}

@sio.event
def connect():
    print("[API] Connected to server!")

@sio.event
def disconnect():
    print("[API] Disconnected from server!")

@sio.event
def connect_error(data):
    print(f"[API] Connection error: {data}")

def process_serial_data():
    """Thread that reads from serial queue and updates sensor data."""
    while True:
        try:
            # Non-blocking read from serial queue
            serial_line = serial_queue.get(timeout=1)

            # Parse serial data (adjust parsing based on your ESP32 output format)
            try:
                # Example: If ESP32 sends "BPM:75" format
                if "BPM:" in serial_line or "bpm:" in serial_line:
                    bpm_value = int(serial_line.split(":")[1].strip())

                    with data_lock:
                        current_sensor_data["bpm"] = bpm_value
                        current_sensor_data["last_updated"] = datetime.now().isoformat()

                    print(f"[SENSOR] BPM updated: {bpm_value}")
                else:
                    # Generic parsing: try to extract first number
                    numbers = re.findall(r'\d+', serial_line)
                    if numbers:
                        with data_lock:
                            current_sensor_data["value"] = int(numbers[0])
                            current_sensor_data["last_updated"] = datetime.now().isoformat()
            except (ValueError, IndexError) as e:
                print(f"[SENSOR] Error parsing serial data '{serial_line}': {e}")

        except Empty:
            # Queue is empty, continue waiting
            continue
        except Exception as e:
            print(f"[SENSOR] Error processing serial data: {e}")
            time.sleep(0.1)

def get_data_packet():
    """Generate a JSON data packet to send."""
    with data_lock:
        sensor_data = current_sensor_data.copy()

    return {
        "timestamp": datetime.now().isoformat(),
        "device": "raspberry_pi_zero_2",
        "data": {
            "bpm": sensor_data["bpm"],
            "value": sensor_data["value"],
            "last_sensor_update": sensor_data["last_updated"]
        }
    }

def send_data_thread():
    """Thread that sends data packets to the API 10 times per second."""
    interval = 0.1  # 100ms = 10 times per second
    
    while True:
        try:
            if sio.connected:
                packet = get_data_packet()
                sio.emit("data", packet)
                print(f"[API] Sent: BPM={packet['data']['bpm']}")
            time.sleep(interval)
        except Exception as e:
            print(f"[API] Error sending data: {e}")
            time.sleep(interval)

def main():
    try:
        print(f"[MAIN] Starting concurrent data acquisition and transmission...")
        print(f"[MAIN] Connecting to {SERVER_URL}...")
        #
        # # Start serial reading thread
        # start_serial_thread()
        # time.sleep(0.5)

        # Connect to the API server
        sio.connect(
            SERVER_URL,
            wait_timeout=10,
            namespaces = ['/']
        )
        
        # # Start sensor data processing thread
        # sensor_thread = threading.Thread(target=process_serial_data, daemon=True)
        # sensor_thread.start()
        # print("[MAIN] Sensor processing thread started")

        # # Start data sending thread
        # sender_thread = threading.Thread(target=send_data_thread, daemon=True)
        # sender_thread.start()
        # print("[MAIN] Data sender thread started")

        # Keep the main thread alive
        print("[MAIN] All threads running. Press Ctrl+C to shutdown...")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down...")
    except Exception as e:
        print(f"[MAIN] Error: {e}")
    finally:
        if sio.connected:
            sio.disconnect()
        print("[MAIN] Cleanup complete")

if __name__ == "__main__":
    main()

