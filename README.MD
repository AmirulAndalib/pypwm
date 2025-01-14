# PyPWM - Raspberry Pi PWM Fan Controller

A sophisticated Python-based PWM fan controller for Raspberry Pi that provides intelligent temperature-based fan speed control with extensive monitoring capabilities.

## Features

- **Intelligent Fan Control**
    - Temperature-based PWM fan speed adjustment
    - Smooth speed ramping
    - Configurable temperature thresholds
    - System load consideration for optimal cooling
    - Customizable fan curves
    - Multiple operation modes (Silent, Performance, Custom)

- **Monitoring & Analytics**
    - Real-time temperature and performance monitoring
    - Historical data collection via SQLite
    - Web-based status interface
    - Temperature trend analysis
    - Performance statistics tracking
    - Email notifications for critical events
    - REST API for external integrations

- **Maintenance & Safety**
    - Automatic maintenance cycles
    - Emergency temperature protection
    - Predictive maintenance analysis
    - Graceful shutdown handling
    - Comprehensive logging system
    - Self-diagnostic checks
    - Failsafe mode

## Quick Start

```bash
git clone https://github.com/amirulandalib/pypwm.git && cd pypwm
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python setup.py install
```

## Detailed Installation

1. Clone the repository:
```bash
git clone https://github.com/amirulandalib/pypwm.git
cd pypwm
```

2. Create and activate virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment:
```bash
# Create .env file with:
BASE_DIR=/home/raspberrypi/Scripts/pypwm/
PORT=8999
LOG_LEVEL=INFO
NOTIFICATION_EMAIL=your@email.com
```

5. Install service:
```bash
sudo cp pwmfan.service /etc/systemd/system/
sudo systemctl enable pwmfan
sudo systemctl start pwmfan
```

## Hardware Setup

### Required Components
- Raspberry Pi (any model)
- PWM-compatible cooling fan
- Appropriate power supply
- Optional: Temperature sensors

### Connection Guide
1. Connect fan's PWM wire to GPIO pin 14 (BCM mode)
2. Connect fan's power (red) to 5V
3. Connect ground (black) to GND
4. Verify connections using provided test script:
```bash
python tests/hardware_test.py
```

## Advanced Configuration

### Temperature Thresholds
Configure in `config.json`:
```json
{
    "thresholds": {
        "critical": 60.0,
        "high": 45.0,
        "medium": 40.0,
        "low_1": 35.0,
        "low_2": 30.0,
        "hysteresis": 2.0
    },
    "fan_curves": {
        "silent": {
            "min_speed": 20,
            "max_speed": 80
        },
        "performance": {
            "min_speed": 40,
            "max_speed": 100
        }
    }
}
```

### API Endpoints
- `/status` - Current system status
- `/metrics` - Performance metrics


## Monitoring & Analytics

### Web Interface
- Status Dashboard: `http://your-pi-ip:8999/status`
- Metrics API: `http://your-pi-ip:8999/metrics`
- Configuration Panel: `http://your-pi-ip:8999/config`

### Data Collection
- Temperature logs
- Fan speed history
- System performance metrics
- Power consumption estimates

## Troubleshooting

Common issues and solutions:
1. Fan not spinning
   - Check GPIO connections
   - Verify power supply
   - Test PWM signal
2. High temperatures
   - Check thermal paste
   - Verify fan curve settings
   - Clean dust/debris

## Development

### Setting up development environment
```bash
pip install -r requirements-dev.txt
pre-commit install
```

### Running tests
```bash
pytest tests/
```

## License & Legal

This project is licensed under the MIT License - see LICENSE file for details.

## Support & Community

- GitHub Issues: Bug reports & feature requests
- Wiki: Extended documentation
- Discussions: Community forum
- Email: Technical support

## Acknowledgements

- RPi.GPIO team
- SQLite developers
- Python community
- All contributors
