import time
import os
from dotenv import load_dotenv
import io
import base64
import numpy as np
from datetime import datetime
from collections import deque
from picamera2 import Picamera2
import cv2
import google.generativeai as genai
import threading

load_dotenv()

# Configuration
CAPTURE_INTERVAL = 0.4  # Capture every 1 second
MIN_SEND_INTERVAL = 15.0  # Minimum 10 seconds between API calls
STABILITY_WINDOW = 3  # Number of stable frames required
CHANGE_THRESHOLD = 0.3  # 15% change threshold for detecting environment change
STABILITY_THRESHOLD = 0.1  # 5% threshold for considering scene "stable"
GEMINI_PROMPT = """Analyze the provided image of this environment. Your task is to act as a world-class music producer and interior designer to determine the perfect musical atmosphere for this specific space.

You must respond ONLY with a valid JSON object. Do not include any conversational filler, markdown code blocks (unless requested), or explanations outside of the JSON.

The JSON must follow this structure:
{
  "genre": "string (e.g., 'Lo-fi Jazz', 'Industrial Techno', 'Ambient Neo-Classical')",
  "tempo": "number (BPM range)",
  "instruments": ["array of 3-5 specific instruments"],
  "mood": "string (e.g., 'Sophisticated', 'Cozy', 'Energetic')",
  "visual_reasoning": "A brief (max 50 characters) explanation of why the lighting, textures, or architecture in the photo led to this musical choice."
}

Base your analysis on:
1. Lighting: (e.g., Warm/dim vs. Bright/clinical)
2. Textures: (e.g., Soft fabrics vs. Hard concrete/glass)
3. Space: (e.g., Intimate/cramped vs. Vast/echoey)
4. Activities (e.g. Sports, Studying, Watching Television, etc...)"""

# Initialize Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-2.5-flash')

class ImageAnalyzer:
    def __init__(self):
        self.last_sent_time = 0
        self.last_sent_image = None
        self.recent_features = deque(maxlen=STABILITY_WINDOW + 1)
        self.change_detected = False
        self.stable_count = 0

    def extract_features(self, image):
        """Extract brightness and color histogram features from image."""
        # Convert to different color spaces
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

        # Calculate brightness (mean of grayscale)
        brightness = np.mean(gray) / 255.0

        # Calculate color histogram (normalized)
        hist_h = cv2.calcHist([hsv], [0], None, [16], [0, 180]).flatten()
        hist_s = cv2.calcHist([hsv], [1], None, [16], [0, 256]).flatten()
        hist_v = cv2.calcHist([hsv], [2], None, [16], [0, 256]).flatten()

        # Normalize histograms
        hist_h = hist_h / (hist_h.sum() + 1e-7)
        hist_s = hist_s / (hist_s.sum() + 1e-7)
        hist_v = hist_v / (hist_v.sum() + 1e-7)

        return {
            'brightness': brightness,
            'hist_h': hist_h,
            'hist_s': hist_s,
            'hist_v': hist_v
        }

    def compare_features(self, feat1, feat2):
        """Compare two feature sets and return difference score."""
        if feat1 is None or feat2 is None:
            return 1.0  # Maximum difference if no comparison possible

        # Brightness difference
        brightness_diff = abs(feat1['brightness'] - feat2['brightness'])

        # Histogram differences using chi-square distance
        hist_h_diff = cv2.compareHist(
            feat1['hist_h'].astype(np.float32),
            feat2['hist_h'].astype(np.float32),
            cv2.HISTCMP_CHISQR_ALT
        )
        hist_s_diff = cv2.compareHist(
            feat1['hist_s'].astype(np.float32),
            feat2['hist_s'].astype(np.float32),
            cv2.HISTCMP_CHISQR_ALT
        )
        hist_v_diff = cv2.compareHist(
            feat1['hist_v'].astype(np.float32),
            feat2['hist_v'].astype(np.float32),
            cv2.HISTCMP_CHISQR_ALT
        )

        # Combine differences (weighted average)
        total_diff = (
            brightness_diff * 0.3 +
            min(hist_h_diff, 1.0) * 0.25 +
            min(hist_s_diff, 1.0) * 0.2 +
            min(hist_v_diff, 1.0) * 0.25
        )

        return min(total_diff, 1.0)

    def is_scene_stable(self):
        """Check if recent frames are stable (not changing much)."""
        if len(self.recent_features) < STABILITY_WINDOW:
            return False

        recent = list(self.recent_features)[-STABILITY_WINDOW:]

        # Compare consecutive frames in the stability window
        for i in range(len(recent) - 1):
            diff = self.compare_features(recent[i], recent[i + 1])
            if diff > STABILITY_THRESHOLD:
                return False

        return True

    def should_send_to_api(self, current_features):
        """Determine if we should send the current image to Gemini API."""
        current_time = time.time()
        time_since_last_send = current_time - self.last_sent_time

        # Always send if minimum interval has passed
        if time_since_last_send >= MIN_SEND_INTERVAL:
            return True, "scheduled"

        # Check for significant change from last sent image
        if self.last_sent_image is not None:
            diff_from_last_sent = self.compare_features(
                current_features,
                self.last_sent_image
            )

            if diff_from_last_sent > CHANGE_THRESHOLD:
                if not self.change_detected:
                    self.change_detected = True
                    self.stable_count = 0
                    print(f"Change detected! Difference: {diff_from_last_sent:.3f}")

                # Check if scene has stabilized after change
                if self.is_scene_stable():
                    self.stable_count += 1
                    if self.stable_count >= 2:  # Require 2 consecutive stable checks
                        self.change_detected = False
                        self.stable_count = 0
                        return True, "stable_change"
                else:
                    self.stable_count = 0

        return False, None

    def process_image(self, image):
        """Process a captured image and decide whether to send to API."""
        features = self.extract_features(image)
        self.recent_features.append(features)

        should_send, reason = self.should_send_to_api(features)

        if should_send:
            self.last_sent_time = time.time()
            self.last_sent_image = features
            return True, reason

        return False, None


def image_to_base64(image):
    """Convert OpenCV image to base64 string."""
    _, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buffer).decode('utf-8')


def send_to_gemini(image):
    """Send image to Gemini API for analysis."""
    try:
        # Convert image to bytes
        _, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        image_bytes = buffer.tobytes()

        # Create image part for Gemini
        image_part = {
            "mime_type": "image/jpeg",
            "data": image_bytes
        }

        # Send to Gemini
        response = model.generate_content([GEMINI_PROMPT, image_part])
        return response.text
    except Exception as e:
        print(f"Error sending to Gemini: {e}")
        return None


def get_gemini_response(timestamp, image):
    response = send_to_gemini(image)

    if response:
        print(f"[{timestamp}] Gemini Response:")
        print("-" * 30)
        print(response[:500] + "..." if len(response) > 500 else response)
        print("-" * 30)
    else:
        print(f"[{timestamp}] Failed to get response from Gemini")


def main():
    print("Initializing camera...")

    # Initialize PiCamera2
    picam2 = Picamera2()
    config = picam2.create_still_configuration(
        main={"size": (1280, 720), "format": "RGB888"}
    )
    picam2.configure(config)
    picam2.start()

    # Wait for camera to warm up
    time.sleep(2)

    analyzer = ImageAnalyzer()
    frame_count = 0

    print("Starting capture loop...")
    print(f"- Capturing every {CAPTURE_INTERVAL}s")
    print(f"- Sending to API every {MIN_SEND_INTERVAL}s minimum")
    print(f"- Change threshold: {CHANGE_THRESHOLD*100}%")
    print(f"- Stability threshold: {STABILITY_THRESHOLD*100}%")
    print("-" * 50)

    try:
        while True:
            # Capture image
            image = picam2.capture_array()
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            frame_count += 1

            timestamp = datetime.now().strftime("%H:%M:%S")

            # Process image and check if we should send to API
            should_send, reason = analyzer.process_image(image)
            if should_send:
                print(f"[{timestamp}] Frame {frame_count}: Sending to Gemini ({reason})...")
                response_thread = threading.Thread(target=get_gemini_response(timestamp, image), daemon=True)
                response_thread.start()
            else:
                status = "change detected, waiting for stability" if analyzer.change_detected else "monitoring"
                print(f"[{timestamp}] Frame {frame_count}: {status}")

            # Wait for next capture
            time.sleep(CAPTURE_INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        picam2.stop()
        print("Camera stopped.")


if __name__ == "__main__":
    main()

