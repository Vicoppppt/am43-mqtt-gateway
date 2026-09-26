"""BLE Worker using Bleak and asyncio.Queue.

Serializes BLE interactions to avoid race conditions and locks on the host Bluetooth adapter.
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import logging
from typing import Callable, Awaitable

from bleak import BleakClient
from bleak.exc import BleakError

from src.am43 import (
    WRITE_CHAR_UUID,
    NOTIFY_CHAR_UUID,
    parse_notification,
    build_position_query_frame,
    DecodedNotification,
)

logger = logging.getLogger(__name__)


@dataclass(order=True)
class BleCommandTask:
    priority: int  # 1 = Commande utilisateur urgente, 10 = Sondage batterie tâche de fond
    device_id: str = field(compare=False)
    mac_address: str = field(compare=False)
    payload: bytes = field(compare=False)
    description: str = field(compare=False)
    wait_after_send: float = field(default=1.0, compare=False)
    track_movement: bool = field(default=False, compare=False)
    target_pos: int | None = field(default=None, compare=False)
    max_track_duration: float = field(default=40.0, compare=False)
    poll_interval: float = field(default=2.0, compare=False)


NotificationCallback = Callable[[str, DecodedNotification], Awaitable[None]]


class BleQueueWorker:
    def __init__(
        self,
        notification_callback: NotificationCallback,
        connect_timeout: float = 12.0,
        max_retries: int = 2,
    ) -> None:
        self.queue: asyncio.PriorityQueue[BleCommandTask] = asyncio.PriorityQueue()
        self.notification_callback = notification_callback
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries
        self._running = False
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background queue processor task."""
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._process_queue(), name="BleQueueWorker")
            logger.info("BLE Queue Worker started.")

    async def stop(self) -> None:
        """Stop the background queue processor task gracefully."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info("BLE Queue Worker stopped.")

    async def enqueue(self, task: BleCommandTask) -> None:
        """Enqueue a new BLE command task."""
        await self.queue.put(task)
        logger.info(
            "[%s] Enqueued command: '%s' (Queue size: %d)",
            task.device_id,
            task.description,
            self.queue.qsize(),
        )

    async def _process_queue(self) -> None:
        """Continuously pull and execute tasks from the queue."""
        while self._running:
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._execute_task(task)
            except Exception as exc:
                logger.exception("[%s] Unexpected error processing task: %s", task.device_id, exc)
            finally:
                self.queue.task_done()

    async def _execute_task(self, task: BleCommandTask) -> None:
        """Execute a single BLE task with connection retries."""
        logger.info(
            "[%s] Executing task '%s' towards %s (payload: %s)",
            task.device_id,
            task.description,
            task.mac_address,
            task.payload.hex(),
        )

        for attempt in range(1, self.max_retries + 1):
            client = None
            try:
                client = BleakClient(task.mac_address, timeout=self.connect_timeout)
                logger.debug("[%s] Connecting (attempt %d/%d)...", task.device_id, attempt, self.max_retries)
                await client.connect()

                if not client.is_connected:
                    raise BleakError("Failed to establish BLE connection")

                logger.debug("[%s] Connected. Subscribing to notifications...", task.device_id)

                async def _on_notify(_sender: int, data: bytearray) -> None:
                    raw_bytes = bytes(data)
                    decoded = parse_notification(raw_bytes)
                    logger.info(
                        "[%s] Received BLE notification: %s (cmd=0x%02X, pos=%s, batt=%s)",
                        task.device_id,
                        raw_bytes.hex(),
                        decoded.cmd if decoded.cmd is not None else 0,
                        decoded.position,
                        decoded.battery,
                    )
                    if decoded.position is not None:
                        setattr(self, f"_last_pos_{task.device_id}", decoded.position)
                    try:
                        await self.notification_callback(task.device_id, decoded)
                    except Exception as err:
                        logger.error("[%s] Notification callback error: %s", task.device_id, err)

                def _sync_notify_wrapper(sender: int, data: bytearray) -> None:
                    # Bleak callbacks run synchronously in the event loop thread
                    asyncio.create_task(_on_notify(sender, data))

                await client.start_notify(NOTIFY_CHAR_UUID, _sync_notify_wrapper)

                logger.debug("[%s] Sending GATT command...", task.device_id)
                await client.write_gatt_char(WRITE_CHAR_UUID, task.payload, response=True)
                logger.info("[%s] Command '%s' sent successfully.", task.device_id, task.description)

                if task.track_movement:
                    logger.info(
                        "[%s] Tracking movement live (polling every %.1fs, target=%s)...",
                        task.device_id,
                        task.poll_interval,
                        task.target_pos,
                    )
                    query_frame = build_position_query_frame()
                    start_time = asyncio.get_running_loop().time()
                    last_observed_pos: int | None = None
                    stable_pos_count = 0

                    # Let the motor start moving
                    await asyncio.sleep(2.0)

                    while (asyncio.get_running_loop().time() - start_time) < task.max_track_duration:
                        try:
                            # Send position query frame
                            await client.write_gatt_char(WRITE_CHAR_UUID, query_frame, response=True)
                        except Exception as poll_err:
                            logger.debug("[%s] Position poll error: %s", task.device_id, poll_err)

                        await asyncio.sleep(task.poll_interval)

                        current_pos = getattr(self, f"_last_pos_{task.device_id}", None)
                        if current_pos is not None:
                            if task.target_pos is not None and abs(current_pos - task.target_pos) <= 1:
                                logger.info("[%s] Target position reached: %d%%.", task.device_id, current_pos)
                                break

                            if current_pos == last_observed_pos:
                                stable_pos_count += 1
                                if stable_pos_count >= 2:
                                    logger.info("[%s] Motor stopped at position: %d%%.", task.device_id, current_pos)
                                    break
                            else:
                                stable_pos_count = 0
                                last_observed_pos = current_pos

                elif task.wait_after_send > 0:
                    await asyncio.sleep(task.wait_after_send)

                await client.stop_notify(NOTIFY_CHAR_UUID)
                # Success!
                return

            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                logger.warning(
                    "[%s] BLE attempt %d/%d failed: %s",
                    task.device_id,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(2.0 * attempt)
            finally:
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                        logger.debug("[%s] Disconnected cleanly.", task.device_id)
                    except Exception as disc_err:
                        logger.debug("[%s] Disconnect error: %s", task.device_id, disc_err)

        logger.error(
            "[%s] Failed to execute task '%s' after %d attempts.",
            task.device_id,
            task.description,
            self.max_retries,
        )
