import json

import socketio
import time
import threading
from queue import Empty
from multiprocessing import Process, Queue as MPQueue

from cam_process import change_num_instruments, main as camera_main, create_shared_num_instruments, create_shared_trigger_capture
# Import the serial queue from gpio_in
from gpio_in import serial_queue, start_serial_thread

# Create a Socket.IO client
sio = socketio.Client()

console_counter = 1

# Server URL
SERVER_URL = "https://conanima.pynekoyne.com"
SOCKET_PATH = "/"

# GSR stability tracking
GSR_CHANGE_THRESHOLD = 15  # Minimum GSR change to consider "drastic"
GSR_STABILITY_WINDOW = 5   # Number of readings to check for stability
GSR_STABILITY_THRESHOLD = 5  # Max variation to consider "stable"
gsr_history = []
last_stable_gsr = None
gsr_change_detected = False

# Shared data state (thread-safe with lock)
data_lock = threading.Lock()
current_sensor_data = {
    "bpm": 0,
    "gsr": 0,
    "temp": 0,
}

# Camera process control
camera_lock = threading.Lock()
camera_process = None
camera_running = False

# Shared value for num_instruments (shared between main process and camera subprocess)
shared_num_instruments = create_shared_num_instruments(3)

# Shared flag to trigger immediate camera capture
shared_trigger_capture = create_shared_trigger_capture()

# Queue for receiving Gemini responses from camera process
gemini_response_queue = MPQueue()

@sio.event
def connect():
    print("[API] Connected to server!")

@sio.event
def disconnect():
    print("[API] Disconnected from server!")

@sio.event
def connect_error(data):
    print(f"[API] Connection error: {data}")

@sio.on('start')
def on_start(data=None):
    """Handle start command from backend to start camera script."""
    global camera_process, camera_running
    print(f"[API] Received 'start' command: {data}")

    with camera_lock:
        if camera_running:
            print("[CAMERA] Camera is already running")
            sio.emit('camera_status', {'status': 'already_running'})
            return

        try:
            # Start the camera process using multiprocessing
            camera_process = Process(
                target=camera_main,
                args=(gemini_response_queue, shared_num_instruments, shared_trigger_capture),
                daemon=True
            )
            camera_process.start()
            camera_running = True
            print(f"[CAMERA] Camera script started (PID: {camera_process.pid})")
            sio.emit('camera_status', {'status': 'started', 'pid': camera_process.pid})
        except Exception as e:
            print(f"[CAMERA] Failed to start camera: {e}")
            sio.emit('camera_status', {'status': 'error', 'message': str(e)})

@sio.on('stop')
def on_stop(data=None):
    """Handle stop command from backend to stop camera script."""
    global camera_process, camera_running
    print(f"[API] Received 'stop' command: {data}")

    with camera_lock:
        if not camera_running or camera_process is None:
            print("[CAMERA] Camera is not running")
            sio.emit('camera_status', {'status': 'not_running'})
            return

        try:
            # Terminate the process
            camera_process.terminate()
            camera_process.join(timeout=5)
            if camera_process.is_alive():
                camera_process.kill()
                camera_process.join()
                print("[CAMERA] Camera script force killed")
                sio.emit('camera_status', {'status': 'force_stopped'})
            else:
                print("[CAMERA] Camera script stopped gracefully")
                sio.emit('camera_status', {'status': 'stopped'})
        except Exception as e:
            print(f"[CAMERA] Error stopping camera: {e}")
            sio.emit('camera_status', {'status': 'error', 'message': str(e)})
        finally:
            camera_process = None
            camera_running = False

def process_serial_data():
    """Thread that reads from serial queue and updates sensor data."""
    global current_sensor_data, gsr_history, last_stable_gsr, gsr_change_detected

    while True:
        try:
            # Non-blocking read from serial queue
            serial_line = serial_queue.get(timeout=1)

            # Update current_sensor_data with the received data
            # serial_line is already a dict from gpio_in.py (json.loads)
            with data_lock:
                if isinstance(serial_line, dict):
                    # Update only the keys that exist in the incoming data
                    if "bpm" in serial_line:
                        current_sensor_data["bpm"] = serial_line["bpm"]
                    if "gsr" in serial_line:
                        current_sensor_data["gsr"] = serial_line["gsr"]
                    if "temp" in serial_line:
                        current_sensor_data["temp"] = serial_line["temp"]

            # Adjust instruments based on GSR value and track stability
            try:
                gsr_value = int(serial_line["gsr"])

                # Update num_instruments based on GSR
                old_value = shared_num_instruments.value
                if gsr_value < 30:
                    shared_num_instruments.value = 2
                elif gsr_value < 50:
                    shared_num_instruments.value = 3
                elif gsr_value < 85:
                    shared_num_instruments.value = 5
                else:
                    shared_num_instruments.value = 6
                if old_value != shared_num_instruments.value:
                    print(f"[SENSOR] GSR={gsr_value}, num_instruments changed: {old_value} -> {shared_num_instruments.value}")

                # Track GSR history for stability detection
                gsr_history.append(gsr_value)
                if len(gsr_history) > GSR_STABILITY_WINDOW:
                    gsr_history.pop(0)

                # Check for drastic change
                if last_stable_gsr is not None:
                    gsr_diff = abs(gsr_value - last_stable_gsr)
                    if gsr_diff >= GSR_CHANGE_THRESHOLD and not gsr_change_detected:
                        gsr_change_detected = True
                        print(f"[GSR] Drastic change detected: {last_stable_gsr} -> {gsr_value} (diff={gsr_diff})")

                # Check if GSR has stabilized after a drastic change
                if gsr_change_detected and len(gsr_history) >= GSR_STABILITY_WINDOW:
                    gsr_range = max(gsr_history) - min(gsr_history)
                    if gsr_range <= GSR_STABILITY_THRESHOLD:
                        # GSR has stabilized, trigger camera capture
                        print(f"[GSR] Stabilized at ~{gsr_value} (range={gsr_range}). Triggering camera capture!")
                        shared_trigger_capture.value = 1
                        gsr_change_detected = False
                        last_stable_gsr = gsr_value

                # Initialize last_stable_gsr if not set
                if last_stable_gsr is None and len(gsr_history) >= GSR_STABILITY_WINDOW:
                    gsr_range = max(gsr_history) - min(gsr_history)
                    if gsr_range <= GSR_STABILITY_THRESHOLD:
                        last_stable_gsr = gsr_value
                        print(f"[GSR] Initial stable value: {last_stable_gsr}")

            except (ValueError, IndexError, KeyError) as e:
                print(f"[SENSOR] Error adjusting instruments: {e}")

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
        "data": {
            "bpm": sensor_data["bpm"],
            "gsr": sensor_data["gsr"],
            "temp": sensor_data["temp"],
        }
    }

def send_data_thread():
    """Thread that sends data packets to the API 10 times per second."""
    interval = 0.1  # 100ms = 10 times per second
    
    while True:
        try:
            if sio.connected:
                packet = get_data_packet()
                global console_counter
                console_counter += 1
                if console_counter > 10:
                    print(f"[API] Packet: {str(json.dumps(packet))}")
                    console_counter = 0

                sio.emit("receiveBioPacket", str(json.dumps(packet)))
            time.sleep(interval)
        except Exception as e:
            print(f"[API] Error sending data: {e}")
            time.sleep(interval)

def gemini_response_emitter():
    """Thread that monitors Gemini response queue and emits to backend."""
    while True:
        try:
            # Check for Gemini responses (non-blocking with timeout)
            try:
                response_data = gemini_response_queue.get(timeout=0.5)
                if sio.connected:
                    sio.emit('camera_data', response_data[7:-3])
                    print(f"[API] Emitted Gemini response to backend")
                else:
                    print(f"[API] Not connected, couldn't emit Gemini response")
            except:
                # Queue empty, continue
                pass
        except Exception as e:
            print(f"[API] Error emitting Gemini response: {e}")
            time.sleep(0.1)

def cleanup():
    """Clean up resources on shutdown."""
    global camera_process, camera_running

    # Stop camera if running
    with camera_lock:
        if camera_running and camera_process is not None:
            try:
                camera_process.terminate()
                camera_process.join(timeout=3)
                if camera_process.is_alive():
                    camera_process.kill()
                    camera_process.join()
            except:
                pass
            camera_process = None
            camera_running = False

    # Disconnect socket
    if sio.connected:
        sio.disconnect()

def main():
    try:
        while True:
            try:
                print(f"[MAIN] Starting concurrent data acquisition and transmission...")
                print(f"[MAIN] Connecting to {SERVER_URL}...")

                # Start serial reading thread
                start_serial_thread()
                time.sleep(0.5)

                # Connect to the API server
                sio.connect(
                    SERVER_URL,
                    wait_timeout=10,
                    namespaces = ['/']
                )

                # Start sensor data processing thread
                sensor_thread = threading.Thread(target=process_serial_data, daemon=True)
                sensor_thread.start()
                print("[MAIN] Sensor processing thread started")

                # Start data sending thread
                sender_thread = threading.Thread(target=send_data_thread, daemon=True)
                sender_thread.start()
                print("[MAIN] Data sender thread started")

                # Start Gemini response emitter thread
                gemini_emitter_thread = threading.Thread(target=gemini_response_emitter, daemon=True)
                gemini_emitter_thread.start()
                print("[MAIN] Gemini response emitter thread started")

                # Keep the main thread alive
                print("[MAIN] All threads running. Press Ctrl+C to shutdown...")
                print("[MAIN] Listening for 'start' and 'stop' commands from server...")
                while True:
                    time.sleep(1)
            except Exception as e:
                print(f"[MAIN] Error: {e}")
            finally:
                cleanup()
                print("[MAIN] Cleanup complete")

    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down...")

if __name__ == "__main__":
    main()

