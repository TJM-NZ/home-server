#!/usr/bin/env python3
import os
import json
import time
import requests
import paho.mqtt.client as mqtt
import subprocess
import threading

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "frigate-alerts")
NTFY_COMMANDS_TOPIC = os.environ.get("NTFY_COMMANDS_TOPIC", f"{NTFY_TOPIC}-cmd")
FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000")
ALERT_OBJECTS = {"person", "car", "dog", "cat"}
PET_OBJECTS = {"dog", "cat"}
PET_COOLDOWN_SECONDS = int(os.environ.get("PET_COOLDOWN_SECONDS", 300))
COOLDOWN_SECONDS = 60
CODEBASE_WATCH_FILE = os.environ.get("CODEBASE_WATCH_FILE", "/app/alerts.py")
CODEBASE_CHECK_INTERVAL = 10  # Check for file changes every 10 seconds
DISK_CHECK_INTERVAL = 300  # Check disk every 5 minutes
DISK_WARNING_THRESHOLD = 80  # Alert when disk usage exceeds 80%
CAMERA_CHECK_INTERVAL = 60  # Check camera health every 1 minute
CAMERA_ERROR_COOLDOWN = 1800  # Alert at most once per 30 minutes per camera

last_alert = {}
last_disk_alert = 0
last_camera_errors = {}  # Track last error time per camera

def send_notification(camera, label, event_id):
    """Send notification to ntfy with snapshot."""
    key = f"{camera}_{label}"
    now = time.time()
    is_pet = label in PET_OBJECTS
    cooldown = PET_COOLDOWN_SECONDS if is_pet else COOLDOWN_SECONDS

    if key in last_alert and (now - last_alert[key]) < cooldown:
        return

    last_alert[key] = now

    title = f"{label.capitalize()} detected"
    message = f"{label.capitalize()} detected on {camera}"
    snapshot_url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg"
    clip_url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"

    headers = {
        "Title": title,
        "Click": clip_url,
    }

    if is_pet:
        headers["Tags"] = "dog" if label == "dog" else "cat"
        headers["Priority"] = "low"
    else:
        headers["Priority"] = "default"

    try:
        snapshot = requests.get(snapshot_url, timeout=10)
        if snapshot.status_code == 200:
            headers["Filename"] = "snapshot.jpg"
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=snapshot.content,
                headers=headers,
                timeout=10
            )
            print(f"Sent notification with snapshot: {message}")
        else:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message,
                headers=headers,
                timeout=10
            )
            print(f"Sent notification without snapshot: {message}")
    except Exception as e:
        print(f"Error sending notification: {e}")

def send_disk_notification(usage_percent):
    """Send notification about disk usage."""
    global last_disk_alert
    now = time.time()

    # Cooldown of 1 hour for disk alerts
    if (now - last_disk_alert) < 3600:
        return

    last_disk_alert = now

    title = "⚠️ Frigate Cache Full"
    message = f"Frigate /tmp/cache is {usage_percent}% full. This may cause recording failures."

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message,
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "warning",
            },
            timeout=10
        )
        print(f"Sent disk alert: {message}")
    except Exception as e:
        print(f"Error sending disk notification: {e}")

def check_disk_usage():
    """Check disk usage of /tmp/cache in frigate container."""
    try:
        result = subprocess.run(
            ["docker", "exec", "frigate", "df", "-h", "/tmp/cache"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                # Parse the output: Filesystem Size Used Avail Use% Mounted on
                parts = lines[1].split()
                if len(parts) >= 5:
                    usage_str = parts[4].rstrip('%')
                    usage_percent = int(usage_str)

                    print(f"Frigate cache usage: {usage_percent}%")

                    if usage_percent >= DISK_WARNING_THRESHOLD:
                        send_disk_notification(usage_percent)

                    return usage_percent

    except Exception as e:
        print(f"Error checking disk usage: {e}")

    return None

def disk_monitor_loop():
    """Background thread to monitor disk usage."""
    print("Starting disk monitor thread")
    while True:
        check_disk_usage()
        time.sleep(DISK_CHECK_INTERVAL)

def handle_restart_command():
    """Restart the Frigate container."""
    try:
        print("Received restart command, restarting Frigate...")
        result = subprocess.run(
            ["docker", "restart", "frigate"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            message = "✅ Frigate container restarted successfully"
            print(message)
        else:
            message = f"❌ Failed to restart Frigate: {result.stderr}"
            print(message)

        # Send confirmation notification
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message,
            headers={"Title": "Frigate Restart"},
            timeout=10
        )

    except Exception as e:
        error_msg = f"Error restarting Frigate: {e}"
        print(error_msg)
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=error_msg,
                headers={"Title": "Frigate Restart Failed"},
                timeout=10
            )
        except:
            pass

def handle_clear_cache_command():
    """Restart Frigate to clear the cache."""
    try:
        usage = check_disk_usage()
        if usage is None:
            message = "⚠️ Could not check disk usage"
        else:
            message = f"Cache is at {usage}%, restarting Frigate to clear..."

        print(message)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message,
            headers={"Title": "Clearing Cache"},
            timeout=10
        )

        # Restart Frigate
        handle_restart_command()

    except Exception as e:
        print(f"Error clearing cache: {e}")

def codebase_watch_loop():
    """Background thread to detect codebase updates via file mtime changes."""
    print(f"Starting codebase watcher on: {CODEBASE_WATCH_FILE}")

    try:
        last_mtime = os.path.getmtime(CODEBASE_WATCH_FILE)
    except OSError:
        print(f"Warning: watch file {CODEBASE_WATCH_FILE} not found, watcher disabled")
        return

    while True:
        time.sleep(CODEBASE_CHECK_INTERVAL)
        try:
            current_mtime = os.path.getmtime(CODEBASE_WATCH_FILE)
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                message = f"Codebase updated on server ({CODEBASE_WATCH_FILE} changed)"
                print(message)
                try:
                    requests.post(
                        f"https://ntfy.sh/{NTFY_COMMANDS_TOPIC}",
                        data=message,
                        headers={
                            "Title": "Codebase Updated",
                            "Tags": "package",
                        },
                        timeout=10
                    )
                    print("Sent codebase update notification")
                except Exception as e:
                    print(f"Error sending codebase update notification: {e}")
        except OSError:
            pass

def command_listener_loop():
    """Background thread to listen for commands via ntfy."""
    print(f"Starting command listener on topic: {NTFY_COMMANDS_TOPIC}")
    print(f"Send 'restart' to https://ntfy.sh/{NTFY_COMMANDS_TOPIC} to restart Frigate")
    print(f"Send 'clear-cache' to https://ntfy.sh/{NTFY_COMMANDS_TOPIC} to clear cache")

    while True:
        try:
            # Subscribe to ntfy and listen for messages
            response = requests.get(
                f"https://ntfy.sh/{NTFY_COMMANDS_TOPIC}/json",
                stream=True,
                timeout=65  # ntfy timeout is 60s
            )

            for line in response.iter_lines():
                if line:
                    try:
                        msg = json.loads(line.decode('utf-8'))
                        if msg.get('event') == 'message':
                            command = msg.get('message', '').strip().lower()
                            print(f"Received command: {command}")

                            if command == 'restart':
                                handle_restart_command()
                            elif command in ['clear-cache', 'clear_cache', 'clearcache']:
                                handle_clear_cache_command()
                            elif command == 'status':
                                usage = check_disk_usage()
                                status_msg = f"Frigate is running. Cache usage: {usage}%"
                                requests.post(
                                    f"https://ntfy.sh/{NTFY_TOPIC}",
                                    data=status_msg,
                                    headers={"Title": "Frigate Status"},
                                    timeout=10
                                )
                            else:
                                print(f"Unknown command: {command}")

                    except json.JSONDecodeError:
                        pass

        except Exception as e:
            print(f"Command listener error: {e}, reconnecting in 10s...")
            time.sleep(10)

def on_connect(client, userdata, flags, rc, properties=None):
    print(f"Connected to MQTT broker with result code {rc}")
    client.subscribe("frigate/events")
    print("Subscribed to frigate/events")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        event_type = payload.get("type")

        if event_type == "new":
            after = payload.get("after", {})
            label = after.get("label")
            camera = after.get("camera")
            event_id = after.get("id")

            if label in ALERT_OBJECTS and event_id:
                print(f"New event: {label} on {camera}")
                send_notification(camera, label, event_id)

    except Exception as e:
        print(f"Error processing message: {e}")

def main():
    print(f"Starting Frigate Alerts")
    print(f"MQTT: {MQTT_HOST}:{MQTT_PORT}")
    print(f"Ntfy alerts topic: {NTFY_TOPIC}")
    print(f"Ntfy commands topic: {NTFY_COMMANDS_TOPIC}")
    print(f"Alerting on: {ALERT_OBJECTS}")
    print(f"Pet objects: {PET_OBJECTS} (cooldown: {PET_COOLDOWN_SECONDS}s)")
    print(f"Disk check interval: {DISK_CHECK_INTERVAL}s, threshold: {DISK_WARNING_THRESHOLD}%")

    # Start disk monitoring thread
    disk_thread = threading.Thread(target=disk_monitor_loop, daemon=True)
    disk_thread.start()

    # Start codebase watcher thread
    watch_thread = threading.Thread(target=codebase_watch_loop, daemon=True)
    watch_thread.start()

    # Start command listener thread
    cmd_thread = threading.Thread(target=command_listener_loop, daemon=True)
    cmd_thread.start()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            print(f"Connection error: {e}, retrying in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()
