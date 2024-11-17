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
from dataclasses import dataclass
from typing import Optional, List, Dict
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3
from contextlib import contextmanager
import socket
import http.server
import socketserver
from queue import Queue
import os

@dataclass
class TempThresholds:
    """Temperature thresholds configuration"""
    high: float = 45.0
    medium: float = 40.0
    low_1: float = 35.0
    low_2: float = 30.0
    hysteresis: float = 2.0

@dataclass
class MetricsData:
    """Store system metrics"""
    timestamp: datetime
    temperature: float
    fan_speed: int
    cpu_load: float
    memory_usage: float
    disk_usage: float

class DataCollector:
    """Collect and store system metrics"""
    def __init__(self, db_path: str = "/home/amirulandalib/Scripts/pypwm/metrics.db"):
        self.db_path = db_path
        self._init_database()
        self.metrics_queue = Queue()
        self.collection_thread = threading.Thread(target=self._collect_metrics_worker, daemon=True)
        self.collection_thread.start()

    def _init_database(self):
        """Initialize SQLite database"""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        
        with self._get_db_connection() as conn:
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
            # Create index on timestamp
            conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON metrics(timestamp)')

    @contextmanager
    def _get_db_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_metrics(self, metrics: MetricsData):
        """Add metrics to queue"""
        self.metrics_queue.put(metrics)

    def _collect_metrics_worker(self):
        """Worker thread to store metrics in database"""
        while True:
            metrics = self.metrics_queue.get()
            with self._get_db_connection() as conn:
                conn.execute('''
                    INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    metrics.timestamp,
                    metrics.temperature,
                    metrics.fan_speed,
                    metrics.cpu_load,
                    metrics.memory_usage,
                    metrics.disk_usage
                ))
            self.metrics_queue.task_done()

    def get_metrics(self, hours: int = 24) -> List[Dict]:
        """Get metrics for the last n hours"""
        with self._get_db_connection() as conn:
            cursor = conn.execute('''
                SELECT * FROM metrics 
                WHERE timestamp > datetime('now', ?)
                ORDER BY timestamp DESC
            ''', (f'-{hours} hours',))
            return [dict(zip([col[0] for col in cursor.description], row))
                   for row in cursor.fetchall()]

class StatusServer(http.server.SimpleHTTPRequestHandler):
    """Simple HTTP server for status monitoring"""
    def __init__(self, *args, fan_controller=None, **kwargs):
        self.fan_controller = fan_controller
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            metrics = self.fan_controller.get_current_metrics()
            self.wfile.write(json.dumps(metrics).encode())
        elif self.path == '/status':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            
            status_html = self.fan_controller.get_status_page()
            self.wfile.write(status_html.encode())
        else:
            self.send_error(404)

class FanController:
    def __init__(self, gpio_pin: int = 14, pwm_freq: int = 100):
        self.gpio_pin = gpio_pin
        self.pwm_freq = pwm_freq
        self.current_dc = 0
        self.running = True
        self.thresholds = TempThresholds()
        
        # Configuration
        self.config_path = Path("/home/amirulandalib/Scripts/pypwm/config.json")
        self.load_config()
        
        # Metrics collection
        self.data_collector = DataCollector()
        self.temp_history: List[float] = []
        self.speed_history: List[int] = []
        
        # Set up logging
        self.logger = self._setup_logging()
        
        # GPIO setup
        self._setup_gpio()
        
        # Setup signal handlers
        self._setup_signal_handlers()
        
        # Start monitoring server
        self.start_monitoring_server()
        
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

    def load_config(self):
        """Load configuration from file"""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
                self.thresholds = TempThresholds(**config.get('thresholds', {}))
                # Add more configuration loading as needed
            except Exception as e:
                print(f"Error loading config: {e}")
                # Use default values
                self.thresholds = TempThresholds()
        else:
            # Create default config
            self.save_config()

    def save_config(self):
        """Save current configuration to file"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            'thresholds': {
                'high': self.thresholds.high,
                'medium': self.thresholds.medium,
                'low_1': self.thresholds.low_1,
                'low_2': self.thresholds.low_2,
                'hysteresis': self.thresholds.hysteresis
            }
        }
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=4)

    def start_monitoring_server(self):
        """Start HTTP monitoring server"""
        def run_server():
            handler = lambda *args: StatusServer(*args, fan_controller=self)
            with socketserver.TCPServer(("", 8000), handler) as httpd:
                httpd.serve_forever()

        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()

    def get_current_metrics(self) -> Dict:
        """Get current system metrics"""
        return {
            'temperature': self.get_cpu_temp(),
            'fan_speed': self.current_dc,
            'cpu_load': psutil.cpu_percent(),
            'memory_usage': psutil.virtual_memory().percent,
            'disk_usage': psutil.disk_usage('/').percent,
            'performance_stats': self.performance_stats
        }

    def get_status_page(self) -> str:
        """Generate HTML status page"""
        metrics = self.get_current_metrics()
        
        return f"""
        <html>
            <head>
                <title>Fan Control Status</title>
                <meta refresh="5">
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .metric {{ margin: 10px; padding: 10px; border: 1px solid #ccc; }}
                </style>
            </head>
            <body>
                <h1>Fan Control Status</h1>
                <div class="metric">Temperature: {metrics['temperature']}°C</div>
                <div class="metric">Fan Speed: {metrics['fan_speed']}%</div>
                <div class="metric">CPU Load: {metrics['cpu_load']}%</div>
                <div class="metric">Memory Usage: {metrics['memory_usage']}%</div>
                <div class="metric">Disk Usage: {metrics['disk_usage']}%</div>
                <h2>Performance Statistics</h2>
                <div class="metric">
                    Runtime: {metrics['performance_stats']['total_runtime']}<br>
                    Emergency Shutdowns: {metrics['performance_stats']['emergency_shutdowns']}<br>
                    Maintenance Performed: {metrics['performance_stats']['maintenance_performed']}
                </div>
            </body>
        </html>
        """

    def check_maintenance(self):
        """Check if maintenance is needed"""
        if datetime.now() - self._last_maintenance >= self.maintenance_interval:
            self.logger.info("Scheduled maintenance check required")
            self.performance_stats['maintenance_performed'] += 1
            self._last_maintenance = datetime.now()
            
            # Perform maintenance test cycle
            self.perform_maintenance_cycle()

    def perform_maintenance_cycle(self):
        """Perform maintenance test cycle"""
        self.logger.info("Starting maintenance cycle")
        
        # Store current speed
        original_speed = self.current_dc
        
        # Test full range of motion
        test_speeds = [0, 25, 50, 75, 100, 75, 50, 25, 0]
        for speed in test_speeds:
            self.ramp_to_speed(speed)
            time.sleep(2)
            
            # Check if fan responds correctly
            if abs(self.current_dc - speed) > 5:
                self.logger.error(f"Fan not responding correctly at {speed}% speed")
                # Could add notification here
        
        # Restore original speed
        self.ramp_to_speed(original_speed)
        self.logger.info("Maintenance cycle completed")

    def analyze_temperature_trends(self):
        """Analyze temperature trends for predictive maintenance"""
        if len(self.temp_history) > 100:
            avg_temp = statistics.mean(self.temp_history[-100:])
            temp_std = statistics.stdev(self.temp_history[-100:])
            
            if avg_temp > self.thresholds.high:
                self.logger.warning(f"High average temperature: {avg_temp:.1f}°C")
            
            # Check for temperature instability
            if temp_std > 5.0:
                self.logger.warning(f"Unstable temperature patterns detected (std: {temp_std:.1f})")

    def handle_emergency(self, reason: str):
        """Handle emergency situations"""
        self.logger.error(f"Emergency situation: {reason}")
        self.performance_stats['emergency_shutdowns'] += 1
        
        # Set fan to maximum speed
        self.ramp_to_speed(100)
        
        # Could add notification system here
        # self.send_notification(f"Emergency: {reason}")
        
        # Wait for temperature to stabilize
        while self.get_cpu_temp() > self.thresholds.high:
            time.sleep(5)

    def run(self):
        """Main control loop"""
        self.perform_initial_test()
        
        with self.error_handling():
            while self.running:
                # Get system metrics
                temp = self.get_cpu_temp()
                system_load = psutil.cpu_percent()
                memory_usage = psutil.virtual_memory().percent
                disk_usage = psutil.disk_usage('/').percent
                
                # Store metrics
                self.temp_history.append(temp)
                self.speed_history.append(self.current_dc)
                
                # Keep history limited
                if len(self.temp_history) > 1000:
                    self.temp_history = self.temp_history[-1000:]
                    self.speed_history = self.speed_history[-1000:]
                
                # Store metrics in database
                metrics = MetricsData(
                    timestamp=datetime.now(),
                    temperature=temp,
                    fan_speed=self.current_dc,
                    cpu_load=system_load,
                    memory_usage=memory_usage,
                    disk_usage=disk_usage
                )
                self.data_collector.add_metrics(metrics)
                
                # Calculate target speed
                target_dc = self.calculate_fan_speed(temp, system_load)
                
                # Update fan speed if needed
                if target_dc != self.current_dc:
                    self.ramp_to_speed(target_dc)
                
                # Log current status
                self.logger.info(
                    f"CPU Temp: {temp:.1f}°C, Fan speed: {self.current_dc}%, "
                    f"System load: {system_load:.1f}%, Memory: {memory_usage:.1f}%"
                )
                
                # Check for potential issues
                if temp > self.thresholds.high + 10:
                    self.handle_emergency("Critical temperature detected")
                
                # Analyze temperature trends
                self.analyze_temperature_trends()
                
                # Check for maintenance
                self.check_maintenance()
                
                # Update performance stats
                self.performance_stats['total_runtime'] = datetime.now() - self.performance_stats['start_time']
                
                time.sleep(5.0)

if __name__ == "__main__":
    controller = FanController()
    controller.run()