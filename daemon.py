#!/usr/bin/python3

import RPi.GPIO as GPIO
import time
import subprocess
import logging
import logging.handlers
import psutil
import atexit
import signal
import sys
import json
import threading
import statistics
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3
from contextlib import contextmanager
import socket  # Import socket
import http.server
import socketserver
from queue import Queue
import os
from dotenv import load_dotenv
import argparse
import re
import math

# Load environment variables from .env file
load_dotenv()
BASE_DIR = os.getenv('BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.getenv('PORT', 8000))
MAX_TEMP = float(os.getenv('MAX_TEMP', 85.0))
PWM_PIN = int(os.getenv('PWM_PIN', 18))  # Default to 18, but configurable
PWM_FREQ = int(os.getenv('PWM_FREQ', 100))


@dataclass
class TempThresholds:
    """Temperature thresholds configuration"""
    high: float = 45.0
    medium: float = 40.0
    low_1: float = 35.0
    low_2: float = 30.0
    hysteresis: float = 2.0

    def validate(self):
        """Validate threshold values"""
        if not (0 < self.low_2 < self.low_1 < self.medium < self.high < MAX_TEMP):
            raise ValueError("Invalid temperature threshold hierarchy")
        if self.hysteresis <= 0:
            raise ValueError("Hysteresis must be positive")


@dataclass
class MetricsData:
    """Store system metrics"""
    timestamp: datetime
    temperature: float
    fan_speed: int
    cpu_load: float
    memory_usage: float
    disk_usage: float


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class DataCollector:
    """Collect and store system metrics with improved concurrency handling"""

    def __init__(self, db_path: str = os.path.join(BASE_DIR, "metrics.db")):
        self.db_path = db_path
        self._init_database()
        self.metrics_queue = Queue()
        self.collection_thread = threading.Thread(target=self._collect_metrics_worker, daemon=True)
        self.collection_thread.start()
        self.lock = threading.Lock()

    def _init_database(self):
        """Initialize SQLite database with WAL mode for better concurrency"""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        with self._get_db_connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    timestamp DATETIME,
                    temperature REAL,
                    fan_speed INTEGER,
                    cpu_load REAL,
                    memory_usage REAL,
                    disk_usage REAL
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_timestamp 
                ON metrics(timestamp)
            ''')

    @contextmanager
    def _get_db_connection(self):
        """Thread-safe database connection context manager"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def add_metrics(self, data: MetricsData):
        """Add metrics to queue for processing"""
        self.metrics_queue.put(data)

    def _collect_metrics_worker(self):
        """Worker thread to process metrics and store in database"""
        while True:
            data = self.metrics_queue.get()
            try:
                with self._get_db_connection() as conn:
                    conn.execute('''
                        INSERT INTO metrics (timestamp, temperature, fan_speed, cpu_load, memory_usage, disk_usage)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (data.timestamp, data.temperature, data.fan_speed, data.cpu_load, data.memory_usage, data.disk_usage))
            except Exception as e:
                logging.error(f"Error writing to database: {e}")  # Use standard logging
            finally:
                self.metrics_queue.task_done()


    def get_metrics(self, hours: int) -> List[Dict]:
        """Retrieve metrics from the last specified hours, ordered by timestamp."""
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute('''
                    SELECT timestamp, temperature, fan_speed, cpu_load, memory_usage, disk_usage
                    FROM metrics
                    WHERE timestamp >= ?
                    ORDER BY timestamp
                ''', (datetime.now() - timedelta(hours=hours),))
                # Convert rows to dictionaries for JSON serialization
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logging.error(f"Error reading from database: {e}")
            return []


class StatusServer(http.server.SimpleHTTPRequestHandler):
    """Improved HTTP server with authentication and more endpoints"""

    def __init__(self, *args, fan_controller=None, **kwargs):
        self.auth_token = os.getenv('AUTH_TOKEN', '')
        self.fan_controller = fan_controller
        super().__init__(*args, **kwargs)

    def check_auth(self):
        """Basic authentication check"""
        if not self.auth_token:
            return True
        auth_header = self.headers.get('Authorization', '')
        return auth_header == f'Bearer {self.auth_token}'

    def do_GET(self):
        """Handle GET requests with authentication"""
        if not self.check_auth():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return

        if self.path == '/metrics':
            self.handle_metrics()
        elif self.path == '/status':
            self.handle_status()
        elif self.path == '/history':
            self.handle_history()
        else:
            self.send_error(404)

    def handle_metrics(self):
        """Return current metrics in JSON format"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        metrics = self.fan_controller.get_current_metrics()
        self.wfile.write(json.dumps(metrics, cls=EnhancedJSONEncoder).encode())

    def handle_status(self):
        """Return HTML status page"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        status_html = self.fan_controller.get_status_page()
        self.wfile.write(status_html.encode('utf-8'))

    def handle_history(self):
        """Return historical data"""
        try:
            hours = int(self.headers.get('Hours', 24))
            hours = max(1, min(720, hours))  # Limit to 1-720 hours
            data = self.fan_controller.data_collector.get_metrics(hours)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data, cls=EnhancedJSONEncoder).encode())
        except Exception as e:
            self.send_error(500, str(e))


class FanController:
    def __init__(self, gpio_pin: int = PWM_PIN, pwm_freq: int = PWM_FREQ, manual_mode: bool = False):
        self.gpio_pin = gpio_pin
        self.pwm_freq = pwm_freq
        self.current_dc = 0
        self.running = True
        self.thresholds = TempThresholds()
        self.manual_mode = manual_mode
        self.last_temp = 0.0
        self.last_load = 0.0
        self.integral = 0.0
        self.last_time = time.monotonic()

        # Configuration
        self.config_path = Path(os.path.join(BASE_DIR, "config.json"))
        self.load_config()

        # Metrics collection
        self.data_collector = DataCollector()
        self.temp_history: List[float] = []
        self.speed_history: List[int] = []

        # Set up logging
        self.logger = self._setup_logging()

        # GPIO setup with validation
        self._setup_gpio()

        # Setup signal handlers
        self._setup_signal_handlers()

        # Initialize maintenance timer
        self._last_maintenance = datetime.now()
        self.maintenance_interval = timedelta(days=30)

        # Performance metrics
        self.performance_stats = {
            'start_time': datetime.now(),
            'total_runtime': timedelta(),
            'temperature_peaks': [],
            'emergency_shutdowns': 0,
            'maintenance_performed': 0
        }
        self.monitoring_server = None # Initialize monitoring server
        if not self.manual_mode:
            self.start_monitoring_server()

    def load_config(self):
        """Load and validate configuration from file"""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
                # Load thresholds safely with defaults
                thresholds_config = config.get('thresholds', {})
                self.thresholds = TempThresholds(
                    high=thresholds_config.get('high', 45.0),
                    medium=thresholds_config.get('medium', 40.0),
                    low_1=thresholds_config.get('low_1', 35.0),
                    low_2=thresholds_config.get('low_2', 30.0),
                    hysteresis=thresholds_config.get('hysteresis', 2.0)
                )

                self.thresholds.validate()


            except Exception as e:
                self.logger.error(f"Error loading config: {e}, using defaults")
                self.thresholds = TempThresholds()  # Ensure defaults
                self.save_config()  # Save defaults for next time.
        else:
            self.logger.info("No config file found, creating with defaults.")
            self.save_config()  # Create default

    def save_config(self):
        """Save current configuration to file"""
        try:
            config_data = {
                'thresholds': asdict(self.thresholds)  # Correctly serialize
            }
            with open(self.config_path, 'w') as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving config: {e}")


    def _setup_gpio(self):
        """Set up GPIO and PWM with validation"""
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.OUT)
            self.pwm = GPIO.PWM(self.gpio_pin, self.pwm_freq)
            self.pwm.start(0)  # Start with fan OFF
            self.logger.info(f"GPIO setup complete. PWM pin: {self.gpio_pin}, Frequency: {self.pwm_freq}Hz")

        except Exception as e:
            self.logger.error(f"GPIO setup failed: {e}")
            self.handle_emergency("GPIO initialization failure")  # Consistent handling

    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown"""
        signal.signal(signal.SIGINT, self._sigterm_handler)
        signal.signal(signal.SIGTERM, self._sigterm_handler)
        atexit.register(self.cleanup)

    def _sigterm_handler(self, signum, frame):
        """Handle termination signals"""
        self.logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.stop_monitoring_server()
        self.running = False
        self.cleanup()
        sys.exit(0)  # Exit cleanly


    def start_monitoring_server(self):
        """Start the HTTP server for monitoring"""
        # Use ThreadingHTTPServer for handling requests in separate threads.
        class CustomHandler(StatusServer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, fan_controller=self, **kwargs)

        self.monitoring_server = socketserver.ThreadingTCPServer(("", PORT), CustomHandler)  # Use ThreadingTCPServer
        self.monitoring_server.daemon_threads = True  # Allow the server to exit even if threads are still running
        self.server_thread = threading.Thread(target=self.monitoring_server.serve_forever, daemon=True)
        self.server_thread.start()
        self.logger.info(f"Monitoring server started on port {PORT}")


    def stop_monitoring_server(self):
      if self.monitoring_server:
        self.logger.info("Stopping monitoring server.")
        self.monitoring_server.shutdown()  # signal shutdown
        self.monitoring_server.server_close() # close the socket
        self.logger.info("Monitoring server stopped.")


    def get_cpu_temp(self) -> float:
        """Get CPU temperature with retry and validation"""
        for _ in range(3):
            try:
                output = subprocess.check_output(
                    ["vcgencmd", "measure_temp"],
                    stderr=subprocess.STDOUT,
                    universal_newlines=True
                )
                temp_str = re.search(r'temp=([\d.]+)', output).group(1)
                temp = float(temp_str)

                if not (0 <= temp <= MAX_TEMP):
                    raise ValueError(f"Temperature out of range: {temp}°C")

                return temp
            except (subprocess.CalledProcessError, AttributeError, ValueError) as e:
                self.logger.warning(f"Temp read error: {e}, retrying...")
                time.sleep(1)

        self.handle_emergency("Temperature sensor failure")
        return MAX_TEMP  # Safe fallback

    def get_system_load(self) -> float:
        """Gets the average CPU load over the last minute."""
        return psutil.cpu_percent(interval=1)


    def calculate_fan_speed(self, temp: float, load: float) -> int:
        """Calculate fan speed using PID-inspired dynamic adjustment"""
        # PID constants
        Kp = 0.8
        Ki = 0.05
        Kd = 0.1

        now = time.monotonic()
        dt = now - self.last_time
        self.last_time = now

        # Calculate error
        error = max(0, temp - self.thresholds.low_2)

        # PID terms
        self.integral += error * dt
        derivative = (error - (self.last_temp - self.thresholds.low_2)) / dt

        # Calculate base speed
        base_speed = (error * Kp) + (self.integral * Ki) + (derivative * Kd)
        load_factor = 1 + (load / 100)  # 1-2x multiplier based on load

        # Adjust speed with load factor and hysteresis
        target_speed = min(100, max(0, base_speed * load_factor))

        # Apply hysteresis
        if abs(target_speed - self.current_dc) < self.thresholds.hysteresis:
            return self.current_dc

        self.last_temp = temp
        return int(round(target_speed))
    def ramp_to_speed(self, target_dc: int):
        """Smooth speed transition with dynamic step size"""
        if target_dc == self.current_dc:
            return

        step = 1 if target_dc > self.current_dc else -1
        steps = abs(target_dc - self.current_dc)

        # Dynamic sleep time based on number of steps
        sleep_time = max(0.01, min(0.1, 1.0 / steps))

        for dc in range(self.current_dc, target_dc, step):
            try:
                self.pwm.ChangeDutyCycle(dc)
                self.current_dc = dc
                time.sleep(sleep_time)
            except Exception as e:
                self.logger.error(f"PWM error: {e}")
                self.handle_emergency("PWM control failure")
                break

        self.pwm.ChangeDutyCycle(target_dc)
        self.current_dc = target_dc


    def _setup_logging(self):
        """Set up logging with timed rotating files"""
        logger = logging.getLogger('FanController')
        logger.setLevel(logging.DEBUG)

        log_dir = Path(BASE_DIR) / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)

        # Timed rotating file handler (daily)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / 'fan_control.log',
            when='midnight',
            backupCount=7,
            encoding='utf-8'
        )
        file_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(module)s - %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Console handler with color
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '\033[1m%(asctime)s - %(levelname)s - %(message)s\033[0m'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        return logger

    def analyze_temperature_trends(self):
        """Enhanced trend analysis with exponential smoothing"""
        if len(self.temp_history) < 10:
            return

        # Calculate exponential moving average
        alpha = 0.1
        ema = self.temp_history[0]
        for temp in self.temp_history[1:]:
            ema = alpha * temp + (1 - alpha) * ema

        # Check for sustained temperature rise
        window = 10
        if len(self.temp_history) >= window:
            recent = self.temp_history[-window:]
            gradient = (sum(recent[-3:])/3 - sum(recent[:3])/3) / window
            if gradient > 0.5:  # More specific threshold
                self.logger.warning(
                    f"Sustained temperature rise detected: {gradient:.2f}°C/min"
                )

    def perform_maintenance_cycle(self):
        """Enhanced maintenance cycle with bearing wear detection"""
        self.logger.info("Starting comprehensive maintenance cycle")
        original_speed = self.current_dc
        test_results = []


        try:
            for speed in [0, 25, 50, 75, 100]:
                self.ramp_to_speed(speed)
                time.sleep(2)

                # Measure current draw (simulated, as we can't directly measure)
                # This is a placeholder; a real implementation would require additional hardware.
                current = speed / 10 + (0.5 if speed > 0 else 0)  # Simulate current draw
                test_results.append((speed, current))
                # Check if fan is responding as expected.  Raise an error if it isn't.
                if speed > 0 and abs(self.current_dc - speed) > 5:
                  raise RuntimeError(f"Fan stuck at {self.current_dc}%")

            # Analyze bearing wear through current fluctuations
            currents = [c for _, c in test_results if _ > 0]  # Only for when fan is running
            if currents:  # Avoid error if list is empty
                current_std = statistics.stdev(currents)
                if current_std > 0.2:
                    self.logger.warning(f"Potential bearing wear detected (std: {current_std:.2f}A)")
        except Exception as e:
            self.logger.error(f"Maintenance failed: {e}")
        finally:
            self.ramp_to_speed(original_speed) # Go back to original speed.

        self.performance_stats['maintenance_performed'] += 1
        self._last_maintenance = datetime.now()
        self.logger.info("Maintenance cycle completed")



    def get_current_metrics(self) -> Dict:
        """Get current system metrics"""
        temp = self.get_cpu_temp()
        load = self.get_system_load()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        return {
            'timestamp': datetime.now(),
            'temperature': temp,
            'fan_speed': self.current_dc,
            'cpu_load': load,
            'memory_usage': memory.percent,
            'disk_usage': disk.percent
        }

    def get_status_page(self) -> str:
        """Enhanced status page with charts and trends"""
        metrics = self.get_current_metrics()
        history = self.data_collector.get_metrics(1)  # Last hour
        
        # Prepare chart data
        timestamps = [m['timestamp'] for m in history]
        temps = [m['temperature'] for m in history]
        speeds = [m['fan_speed'] for m in history]

        return f"""
        <html>
            <head>
                <title>Fan Control Status</title>
                <meta http-equiv="refresh" content="30">
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    .grid {{ 
                        display: grid;
                        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                        gap: 20px;
                        padding: 20px;
                    }}
                    .card {{ 
                        background: #f5f5f5;
                        padding: 20px;
                        border-radius: 10px;
                        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                    }}
                    canvas {{ max-width: 100%; }}
                </style>
            </head>
            <body>
                <div class="grid">
                    <div class="card">
                        <h2>Real-time Metrics</h2>
                        <p>Temperature: {metrics['temperature']:.1f}°C</p>
                        <p>Fan Speed: {metrics['fan_speed']}%</p>
                        <p>CPU Load: {metrics['cpu_load']:.1f}%</p>
                    </div>
                    
                    <div class="card">
                        <canvas id="tempChart"></canvas>
                    </div>
                    
                    <div class="card">
                        <canvas id="speedChart"></canvas>
                    </div>
                </div>

                <script>
                    const timeLabels = {json.dumps(timestamps)};
                    
                    new Chart(document.getElementById('tempChart'), {{
                        type: 'line',
                        data: {{
                            labels: timeLabels,
                            datasets: [{{
                                label: 'Temperature (°C)',
                                data: {json.dumps(temps)},
                                borderColor: '#ff6384',
                                tension: 0.1
                            }}]
                        }}
                    }});
                    
                    new Chart(document.getElementById('speedChart'), {{
                        type: 'line',
                        data: {{
                            labels: timeLabels,
                            datasets: [{{
                                label: 'Fan Speed (%)',
                                data: {json.dumps(speeds)},
                                borderColor: '#36a2eb',
                                tension: 0.1
                            }}]
                        }}
                    }});
                </script>
            </body>
        </html>
        """

    def handle_emergency(self, reason: str):
      """Handles emergency situations, like sensor failures or overheating."""
      self.logger.critical(f"Emergency shutdown triggered: {reason}")
      self.performance_stats['emergency_shutdowns'] += 1
      self.ramp_to_speed(100)  # Full speed in emergency
      self.running = False
      self.cleanup()
      sys.exit(1) # Exit with error code.


    def set_speed(self, speed: int):
        """Sets the fan speed manually, entering manual mode."""
        if 0 <= speed <= 100:
            self.manual_mode = True  # Ensure manual mode is enabled
            self.ramp_to_speed(speed)
            self.logger.info(f"Fan speed manually set to {speed}%")
        else:
            self.logger.error("Invalid speed.  Must be between 0 and 100.")

    def run(self):
      """Main control loop"""
      self.logger.info("Starting fan controller...")
      try:
          while self.running:
              if not self.manual_mode:
                  temp = self.get_cpu_temp()
                  load = self.get_system_load()
                  target_speed = self.calculate_fan_speed(temp, load)
                  self.ramp_to_speed(target_speed)
                  
                  # Store and analyze temperature history
                  self.temp_history.append(temp)
                  if len(self.temp_history) > 100:
                      self.temp_history.pop(0)  # Keep history to a manageable size
                  self.analyze_temperature_trends()

                  # Check for maintenance
                  if datetime.now() - self._last_maintenance > self.maintenance_interval:
                      self.perform_maintenance_cycle()


              # Collect metrics and add to queue.  This happens in both auto and manual mode.
              metrics = self.get_current_metrics()
              self.data_collector.add_metrics(metrics)

              time.sleep(2)  # Check every 2 seconds
      except KeyboardInterrupt:
        self.logger.info("Keyboard interrupt detected, shutting down.")
        self.running= False
        self.cleanup()
      finally:
          self.cleanup()
          self.logger.info("Fan controller stopped.")



    def cleanup(self):
        """Clean up GPIO and resources"""
        self.logger.info("Performing cleanup...")
        if self.pwm:
            self.pwm.stop()
        GPIO.cleanup()
        self.stop_monitoring_server() # close monitoring server
        self.logger.info("Cleanup complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Advanced Raspberry Pi Fan Controller",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--set-speed', type=int,
                        help='Set manual fan speed (0-100)')
    parser.add_argument('--calibrate', action='store_true',
                        help='Perform full calibration cycle')
    parser.add_argument('--dump-config', action='store_true',
                        help='Display current configuration')
    args = parser.parse_args()

    try:
        controller = FanController(manual_mode=bool(args.set_speed))

        if args.calibrate:
            controller.perform_maintenance_cycle()
            sys.exit(0)

        if args.dump_config:
            print(json.dumps(asdict(controller.thresholds), indent=4))
            sys.exit(0)

        if args.set_speed is not None:
            controller.set_speed(args.set_speed)
            controller.run()  # Continue monitoring in manual mode
        else:
            controller.run()

    except Exception as e:
        logging.error(f"Critical failure: {e}")  # Use the standard logger
        sys.exit(1)