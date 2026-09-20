"""MQTT client management and Home Assistant MQTT Discovery."""

from __future__ import annotations
import json
import logging
from typing import Any, Callable, Dict

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logger = logging.getLogger(__name__)

CommandCallback = Callable[[str, str, str], None]
# callback(device_id, command_type, payload_str)


class MqttManager:
    def __init__(
        self,
        config: Dict[str, Any],
        devices: list[Dict[str, Any]],
        command_callback: CommandCallback,
    ) -> None:
        self.config = config
        self.devices = devices
        self.command_callback = command_callback

        self.broker = config.get("host", "localhost")
        self.port = int(config.get("port", 1883))
        self.username = config.get("username")
        self.password = config.get("password")
        self.base_topic = config.get("base_topic", "am43").rstrip("/")
        self.discovery_prefix = config.get("discovery_prefix", "homeassistant").rstrip("/")

        self.availability_topic = f"{self.base_topic}/status"

        client_id = config.get("client_id", "am43_mqtt_gateway")
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )

        if self.username:
            self.client.username_pw_set(self.username, self.password)

        # Last Will and Testament (LWT)
        self.client.will_set(
            self.availability_topic,
            payload="offline",
            qos=1,
            retain=True,
        )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        """Connect to the MQTT broker and start the network loop."""
        logger.info("Connecting to MQTT broker at %s:%d...", self.broker, self.port)
        self.client.connect_async(self.broker, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        """Publish offline status and disconnect."""
        logger.info("Stopping MQTT client...")
        try:
            self.client.publish(self.availability_topic, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception as exc:
            logger.debug("Error during MQTT disconnect: %s", exc)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code == 0:
            logger.info("Successfully connected to MQTT broker.")
            # Publish online status
            client.publish(self.availability_topic, "online", qos=1, retain=True)

            # Subscribe to command topics for all configured devices
            topic_set = f"{self.base_topic}/+/set"
            topic_set_position = f"{self.base_topic}/+/set_position"
            topic_battery_query = f"{self.base_topic}/+/battery/query"

            client.subscribe([(topic_set, 1), (topic_set_position, 1), (topic_battery_query, 1)])
            logger.info("Subscribed to MQTT topics: %s, %s, %s", topic_set, topic_set_position, topic_battery_query)

            # Publish Home Assistant Discovery configurations
            self.publish_discovery()
        else:
            logger.error("Failed to connect to MQTT broker, reason_code=%s", reason_code)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if reason_code != 0:
            logger.warning("Unexpected MQTT disconnection (reason_code=%s). Automatic reconnect in progress...", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        logger.info("Received MQTT message on %s: %s", topic, payload)

        parts = topic.split("/")
        # Expecting format: {base_topic}/{device_id}/{action}
        if len(parts) >= 3 and parts[0] == self.base_topic:
            device_id = parts[1]
            action = parts[2]
            try:
                self.command_callback(device_id, action, payload)
            except Exception as exc:
                logger.exception("Error executing command callback for device %s: %s", device_id, exc)

    def publish_discovery(self) -> None:
        """Publish Home Assistant MQTT Discovery payloads for covers and battery sensors."""
        for dev in self.devices:
            dev_id = dev["id"]
            name = dev.get("name", dev_id)
            mac = dev["mac"]
            mac_clean = mac.replace(":", "").upper()
            device_info = {
                "identifiers": [f"am43_{mac_clean}"],
                "name": name,
                "model": "AM43 Blind Motor",
                "manufacturer": "A-OK / Generic",
            }

            # 1. Cover Entity
            cover_discovery_topic = f"{self.discovery_prefix}/cover/{dev_id}/config"
            cover_payload = {
                "name": name,
                "unique_id": f"am43_{dev_id}_cover",
                "device_class": "blind",
                "command_topic": f"{self.base_topic}/{dev_id}/set",
                "set_position_topic": f"{self.base_topic}/{dev_id}/set_position",
                "state_topic": f"{self.base_topic}/{dev_id}/state",
                "position_topic": f"{self.base_topic}/{dev_id}/position",
                "payload_open": "OPEN",
                "payload_close": "CLOSE",
                "payload_stop": "STOP",
                "state_open": "open",
                "state_closed": "closed",
                "state_opening": "opening",
                "state_closing": "closing",
                "state_stopped": "stopped",
                "availability_topic": self.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_info,
            }
            self.client.publish(
                cover_discovery_topic,
                json.dumps(cover_payload),
                qos=1,
                retain=True,
            )
            logger.info("Published Home Assistant Discovery for Cover: %s", cover_discovery_topic)

            # 2. Battery Sensor Entity
            battery_discovery_topic = f"{self.discovery_prefix}/sensor/{dev_id}_battery/config"
            battery_payload = {
                "name": f"{name} Batterie",
                "unique_id": f"am43_{dev_id}_battery",
                "device_class": "battery",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "state_topic": f"{self.base_topic}/{dev_id}/battery",
                "availability_topic": self.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_info,
            }
            self.client.publish(
                battery_discovery_topic,
                json.dumps(battery_payload),
                qos=1,
                retain=True,
            )
            logger.info("Published Home Assistant Discovery for Battery: %s", battery_discovery_topic)

    def publish_position(self, device_id: str, position: int) -> None:
        """Publish motor position to MQTT."""
        topic = f"{self.base_topic}/{device_id}/position"
        self.client.publish(topic, str(position), qos=1, retain=True)

    def publish_state(self, device_id: str, state: str) -> None:
        """Publish motor state to MQTT (open, closed, opening, closing, stopped)."""
        topic = f"{self.base_topic}/{device_id}/state"
        self.client.publish(topic, state, qos=1, retain=True)

    def publish_battery(self, device_id: str, battery: int) -> None:
        """Publish motor battery percentage to MQTT."""
        topic = f"{self.base_topic}/{device_id}/battery"
        self.client.publish(topic, str(battery), qos=1, retain=True)
