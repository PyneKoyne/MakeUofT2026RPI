import json

import socketio
import time
import threading
import re
from datetime import datetime
from queue import Empty
from multiprocessing import Process, Queue as MPQueue

from cam_process import change_num_instruments, main as camera_main
# Import the serial queue from gpio_in
from gpio_in import serial_queue, start_serial_thread

# Create a Socket.IO client
sio = socketio.Client()

# Server URL
SERVER_URL = "https://conanima.pynekoyne.com"
SOCKET_PATH = "/"

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
                args=(gemini_response_queue,),
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
    while True:
        try:
            # Non-blocking read from serial queue
            serial_line = serial_queue.get(timeout=1)

            # Parse serial data (adjust parsing based on your ESP32 output format)
            try:
                # Example: If ESP32 sends "BPM:75" format
                global current_sensor_data
                current_sensor_data = serial_line

                if "GSR:" in serial_line or "gsr:" in serial_line:
                    gsr_value = int(serial_line.split(":")[1].strip())
                    if gsr_value < 15:
                        change_num_instruments(1)
                    elif gsr_value < 30:
                        change_num_instruments(2)
                    elif gsr_value < 50:
                        change_num_instruments(3)
                    elif gsr_value < 85:
                        change_num_instruments(5)
                    else:
                        change_num_instruments(6)
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
        "data": {
            sensor_data
        }
    }

def send_data_thread():
    """Thread that sends data packets to the API 10 times per second."""
    interval = 0.1  # 100ms = 10 times per second
    
    while True:
        try:
            if sio.connected:
                packet = get_data_packet()
                print(f"[API] Packet: {str(json.dumps(packet))}")
                sio.emit("data", str(json.dumps(packet)))
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
                    sio.emit('gemini_response', response_data)
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

    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down...")
    except Exception as e:
        print(f"[MAIN] Error: {e}")
    finally:
        cleanup()
        print("[MAIN] Cleanup complete")

if __name__ == "__main__":
    main()

