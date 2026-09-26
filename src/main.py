"""Main application entrypoint for AM43 to MQTT Gateway."""

from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Any, Dict

# S'assurer que la racine du projet est dans sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.am43 import (
    build_set_position_frame,
    build_open_frame,
    build_close_frame,
    build_stop_frame,
    build_battery_query_frame,
    build_position_query_frame,
    DecodedNotification,
)
from src.ble_worker import BleQueueWorker, BleCommandTask
from src.mqtt_client import MqttManager

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("am43_gateway")


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration from YAML file with environment variable overrides."""
    config: Dict[str, Any] = {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "username": None,
            "password": None,
            "base_topic": "am43",
            "discovery_prefix": "homeassistant",
        },
        "devices": [],
        "settings": {
            "connect_timeout": 15.0,
            "max_retries": 2,
            "battery_poll_interval_hours": 4,
        },
    }

    if os.path.exists(config_path):
        logger.info("Loading configuration file: %s", config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}
            if "mqtt" in yaml_config:
                config["mqtt"].update(yaml_config["mqtt"])
            if "devices" in yaml_config:
                config["devices"] = yaml_config["devices"]
            if "settings" in yaml_config:
                config["settings"].update(yaml_config["settings"])
    else:
        logger.warning("Config file %s not found. Using defaults and environment variables.", config_path)

    # Environment variable overrides
    if "MQTT_HOST" in os.environ:
        config["mqtt"]["host"] = os.environ["MQTT_HOST"]
    if "MQTT_PORT" in os.environ:
        config["mqtt"]["port"] = int(os.environ["MQTT_PORT"])
    if "MQTT_USERNAME" in os.environ:
        config["mqtt"]["username"] = os.environ["MQTT_USERNAME"]
    if "MQTT_PASSWORD" in os.environ:
        config["mqtt"]["password"] = os.environ["MQTT_PASSWORD"]
    if "MQTT_BASE_TOPIC" in os.environ:
        config["mqtt"]["base_topic"] = os.environ["MQTT_BASE_TOPIC"]
    if "MQTT_DISCOVERY_PREFIX" in os.environ:
        config["mqtt"]["discovery_prefix"] = os.environ["MQTT_DISCOVERY_PREFIX"]

    return config


class Am43Gateway:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.devices: list[Dict[str, Any]] = config.get("devices", [])
        self.devices_by_id = {d["id"]: d for d in self.devices}
        self.settings = config.get("settings", {})

        self.loop = asyncio.get_running_loop()

        # Initialize BLE Queue Worker
        self.ble_worker = BleQueueWorker(
            notification_callback=self._on_ble_notification,
            connect_timeout=float(self.settings.get("connect_timeout", 15.0)),
            max_retries=int(self.settings.get("max_retries", 2)),
        )

        # Initialize MQTT Manager
        self.mqtt_manager = MqttManager(
            config=config.get("mqtt", {}),
            devices=self.devices,
            command_callback=self._on_mqtt_command,
        )

        self._battery_poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def _on_ble_notification(self, device_id: str, decoded: DecodedNotification) -> None:
        """Handle decoded BLE notifications received from a motor."""
        if decoded.position is not None:
            # The motor native range (0 = open, 100 = closed) is passed to HA natively
            ha_pos = decoded.position
            device = self.devices_by_id.get(device_id)
            if device:
                device["current_pos"] = ha_pos
            logger.info("[%s] Motor reported position: %d%% (HA position: %d%%)", device_id, decoded.position, ha_pos)
            self.mqtt_manager.publish_position(device_id, ha_pos)
            # With position_open: 0 and position_closed: 100:
            state = "closed" if ha_pos == 100 else "open"
            self.mqtt_manager.publish_state(device_id, state)

        if decoded.battery is not None:
            logger.info("[%s] Motor reported battery: %d%%", device_id, decoded.battery)
            self.mqtt_manager.publish_battery(device_id, decoded.battery)

    def _on_mqtt_command(self, device_id: str, action: str, payload: str) -> None:
        """Handle commands received from MQTT broker."""
        device = self.devices_by_id.get(device_id)
        if not device:
            logger.warning("Received command for unknown device ID: '%s'", device_id)
            return

        mac = device["mac"]

        if action == "set":
            cmd = payload.upper()
            if cmd == "OPEN":
                frame = build_open_frame()
                desc = "OPEN (HA 100% / Motor 0%)"
                self.mqtt_manager.publish_state(device_id, "opening")
            elif cmd == "CLOSE":
                frame = build_close_frame()
                desc = "CLOSE (HA 0% / Motor 100%)"
                self.mqtt_manager.publish_state(device_id, "closing")
            elif cmd == "STOP":
                frame = build_stop_frame()
                desc = "STOP"
                self.mqtt_manager.publish_state(device_id, "stopped")
            else:
                logger.warning("[%s] Unrecognized command payload: '%s'", device_id, payload)
                return

            task = BleCommandTask(
                priority=1,
                device_id=device_id,
                mac_address=mac,
                payload=frame,
                description=desc,
                wait_after_send=1.0,
            )
            asyncio.run_coroutine_threadsafe(self.ble_worker.enqueue(task), self.loop)

            async def delayed_query_action():
                await asyncio.sleep(35.0)
                q_frame = build_position_query_frame()
                q_task = BleCommandTask(
                    priority=5,
                    device_id=device_id,
                    mac_address=mac,
                    payload=q_frame,
                    description="DELAYED_POSITION_QUERY_ACTION",
                    wait_after_send=0.5,
                )
                await self.ble_worker.enqueue(q_task)

            asyncio.run_coroutine_threadsafe(delayed_query_action(), self.loop)

        elif action == "set_position":
            try:
                ha_pos = int(round(float(payload)))
                ha_pos = max(0, min(100, ha_pos))
            except ValueError:
                logger.warning("[%s] Invalid position value: '%s'", device_id, payload)
                return

            # Pass through the exact position received from HA
            motor_pos = ha_pos
            frame = build_set_position_frame(motor_pos)
            desc = f"SET_POSITION to HA {ha_pos}% (Motor {motor_pos}%)"
            
            # Optimistic state update based on current direction
            current_pos = device.get("current_pos", 50)
            if ha_pos < current_pos:
                # moving towards 0 (Open)
                predicted_state = "opening"
            elif ha_pos > current_pos:
                # moving towards 100 (Closed)
                predicted_state = "closing"
            else:
                predicted_state = "stopped"
                
            self.mqtt_manager.publish_state(device_id, predicted_state)
            self.mqtt_manager.publish_position(device_id, ha_pos)
            device["current_pos"] = ha_pos

            task = BleCommandTask(
                priority=1,
                device_id=device_id,
                mac_address=mac,
                payload=frame,
                description=desc,
                wait_after_send=1.0,
            )
            asyncio.run_coroutine_threadsafe(self.ble_worker.enqueue(task), self.loop)

            async def delayed_query():
                await asyncio.sleep(35.0)
                q_frame = build_position_query_frame()
                q_task = BleCommandTask(
                    priority=5,
                    device_id=device_id,
                    mac_address=mac,
                    payload=q_frame,
                    description="DELAYED_POSITION_QUERY",
                    wait_after_send=0.5,
                )
                await self.ble_worker.enqueue(q_task)

            asyncio.run_coroutine_threadsafe(delayed_query(), self.loop)

        elif action == "battery":
            frame = build_battery_query_frame()
            desc = "QUERY_BATTERY"
            task = BleCommandTask(
                priority=10,
                device_id=device_id,
                mac_address=mac,
                payload=frame,
                description=desc,
                wait_after_send=0.5,
            )
            asyncio.run_coroutine_threadsafe(self.ble_worker.enqueue(task), self.loop)

    async def _battery_polling_loop(self) -> None:
        """Periodically query battery levels for all configured devices."""
        interval_hours = float(self.settings.get("battery_poll_interval_hours", 4))
        if interval_hours <= 0:
            logger.info("Automatic battery polling is disabled.")
            return

        interval_seconds = interval_hours * 3600
        logger.info("Starting battery polling task (every %.1f hours)...", interval_hours)

        # Initial battery poll after 2 minutes to let the gateway settle
        await asyncio.sleep(120.0)

        while not self._stop_event.is_set():
            for dev in self.devices:
                dev_id = dev["id"]
                mac = dev["mac"]
                frame = build_battery_query_frame()
                task = BleCommandTask(
                    priority=10,
                    device_id=dev_id,
                    mac_address=mac,
                    payload=frame,
                    description="PERIODIC_BATTERY_QUERY",
                    wait_after_send=0.5,
                )
                await self.ble_worker.enqueue(task)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        """Start all services."""
        logger.info("Starting AM43 MQTT Gateway with %d devices...", len(self.devices))
        self.ble_worker.start()
        self.mqtt_manager.start()

        # Initialize device state and query true position
        for dev in self.devices:
            dev_id = dev["id"]
            mac = dev["mac"]
            dev["current_pos"] = 50
            
            # Send initial position query
            frame = build_position_query_frame()
            task = BleCommandTask(
                priority=2,
                device_id=dev_id,
                mac_address=mac,
                payload=frame,
                description="STARTUP_POSITION_QUERY",
                wait_after_send=0.5,
            )
            asyncio.create_task(self.ble_worker.enqueue(task))

        self._battery_poll_task = asyncio.create_task(
            self._battery_polling_loop(),
            name="BatteryPollingLoop",
        )

        await self._stop_event.wait()

    async def stop(self) -> None:
        """Stop all services cleanly."""
        logger.info("Stopping AM43 MQTT Gateway...")
        self._stop_event.set()

        if self._battery_poll_task:
            self._battery_poll_task.cancel()
            try:
                await self._battery_poll_task
            except asyncio.CancelledError:
                pass

        await self.ble_worker.stop()
        self.mqtt_manager.stop()
        logger.info("AM43 MQTT Gateway stopped.")


async def main() -> None:
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    config = load_config(config_path)

    gateway = Am43Gateway(config)

    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("Shutdown signal received.")
        asyncio.create_task(gateway.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # On Windows, add_signal_handler is not fully implemented for all loops
            pass

    try:
        await gateway.start()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("Fatal error in gateway main loop: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
