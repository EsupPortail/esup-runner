# runner/app/managers/process_manager.py
"""
Process manager for multi-instance Uvicorn runner deployment.
Handles process creation, monitoring, and lifecycle management.
"""

import os
import signal
import socket
import sys
import time
from multiprocessing import Process
from typing import Dict, List

from app.core.config import config, reload_config_from_env
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()


def _select_gpu_for_instance(runner_instance_id: int, devices_csv: str) -> str:
    """Pick a GPU index deterministically from the instance id."""

    devices = [d.strip() for d in (devices_csv or "").split(",") if d.strip()]
    if not devices:
        return ""

    gpu_idx = runner_instance_id % len(devices)
    return devices[gpu_idx]


def run_uvicorn_instance(runner_instance_id: int, runner_instance_port: int) -> None:
    """Entry point for a spawned Uvicorn process.

    Defined at module scope so ``multiprocessing`` can pickle it when using the
    ``spawn`` start method (e.g. on some Linux setups or inside containers).
    """

    try:
        os.environ["RUNNER_INSTANCE_ID"] = str(runner_instance_id)
        os.environ["RUNNER_PORT"] = str(runner_instance_port)
        # Refresh config after setting instance env vars so grouped task types are
        # resolved per instance, not as the launcher union.
        instance_config = reload_config_from_env()
        os.environ["RUNNER_INSTANCE_URL"] = (
            f"{instance_config.RUNNER_PROTOCOL}://{instance_config.RUNNER_HOST}:{runner_instance_port}"
        )

        # Dynamic GPU selection per runner instance (GPU mode only)
        if instance_config.ENCODING_TYPE == "GPU":
            devices_csv = os.getenv(
                "GPU_CUDA_VISIBLE_DEVICES", str(instance_config.GPU_CUDA_VISIBLE_DEVICES)
            )
            selected_gpu = _select_gpu_for_instance(runner_instance_id, devices_csv)

            if selected_gpu:
                # Update environment for child processes (ffmpeg/whisper)
                os.environ["GPU_CUDA_VISIBLE_DEVICES"] = selected_gpu
                os.environ["CUDA_VISIBLE_DEVICES"] = selected_gpu
                # When exposing a single GPU via CUDA_VISIBLE_DEVICES, the ordinal seen by
                # ffmpeg/torch inside the process becomes 0.
                os.environ["GPU_HWACCEL_DEVICE"] = "0"

                # Also patch in-process config so handlers pass the right flags
                instance_config.GPU_HWACCEL_DEVICE = 0
                instance_config.GPU_CUDA_VISIBLE_DEVICES = selected_gpu

                logger.info(
                    "Instance %s on port %s bound to GPU %s (from %s)",
                    runner_instance_id,
                    runner_instance_port,
                    selected_gpu,
                    devices_csv,
                )

        # Import inside the subprocess for isolation
        import uvicorn

        from app.core.setup_logging import get_uvicorn_log_config

        log_config = get_uvicorn_log_config(
            runner_instance_id=runner_instance_id,
            json_format=False,
        )

        logger.info(
            f"Starting Uvicorn instance {runner_instance_id} on port {runner_instance_port}"
        )

        uvicorn.run(
            "app.main:app",
            host=instance_config.RUNNER_HOST,
            port=runner_instance_port,
            reload=False,
            log_config=log_config,
            access_log=True,
            workers=1,
        )
    except Exception as e:  # pragma: no cover - defensive logging
        logger.error(f"Uvicorn instance {runner_instance_id} failed: {e}")


class UvicornProcessManager:
    """
    Manages multiple Uvicorn processes with independent configurations.

    Each process runs in complete isolation, so failures in one instance
    do not affect others. This provides high availability and fault tolerance.
    """

    def __init__(self, base_port: int = 8000, instances: int = 1):
        """
        Initialize process manager.

        Args:
            base_port: Starting port number for the first instance
            instances: Number of runner instances to launch
        """
        self.base_port = base_port
        self.instances = instances
        self.processes: List[Process] = []
        self.ports: List[int] = []

        # Generate port list
        self.ports = [base_port + i for i in range(instances)]

        logger.info(
            f"UvicornProcessManager initialized with {instances} instances on ports {self.ports}"
        )

    def _find_available_port(self, start_port: int) -> int:
        """
        Find an available port starting from the specified port.

        Args:
            start_port: Port to start checking from

        Returns:
            int: First available port number
        """
        port = start_port
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("localhost", port))
                    return port
            except OSError:
                port += 1

    def _create_uvicorn_process(
        self, runner_instance_port: int, runner_instance_id: int
    ) -> Process:
        """
        Create a Uvicorn process with isolated configuration.

        Args:
            runner_instance_port: Port number for this instance
            runner_instance_id: Unique identifier for this runner instance

        Returns:
            Process: Configured Uvicorn process
        """

        return Process(
            target=run_uvicorn_instance,
            args=(runner_instance_id, runner_instance_port),
            name=f"{config.RUNNER_BASE_NAME}-{runner_instance_id}",
            daemon=False,  # Non-daemon so main process waits for children
        )

    def start_all_instances(self) -> None:
        """
        Start all Uvicorn instances as separate processes.
        """
        logger.info(f"Starting {self.instances} runner instances...")

        for i, port in enumerate(self.ports):
            # Ensure port is available
            available_port = self._find_available_port(port)
            if available_port != port:
                logger.warning(f"Port {port} busy, using {available_port} for instance {i}")
                self.ports[i] = available_port

            # Create and start process
            process = self._create_uvicorn_process(self.ports[i], i)
            process.start()
            self.processes.append(process)

            logger.info(f"Started runner instance {i} (PID: {process.pid}) on port {self.ports[i]}")

            # Small delay to avoid port conflicts during startup
            time.sleep(0.5)

    def stop_all_instances(self) -> None:
        """
        Stop all running Uvicorn instances gracefully.
        """
        logger.info("Stopping all runner instances...")

        for i, process in enumerate(self.processes):
            if process.is_alive():
                logger.info(f"Stopping instance {i} (PID: {process.pid})")
                process.terminate()
                process.join(timeout=10)  # Wait up to 10 seconds

                if process.is_alive():
                    logger.warning(f"Instance {i} did not terminate gracefully, forcing...")
                    process.kill()
                    process.join()

        self.processes.clear()
        logger.info("All runner instances stopped")

    def restart_instance(self, runner_instance_id: int) -> bool:
        """
        Restart a specific runner instance.

        Args:
            runner_instance_id: ID of instance to restart

        Returns:
            bool: True if restart was successful
        """
        if runner_instance_id >= len(self.processes):
            logger.error(f"Cannot restart instance {runner_instance_id}: out of range")
            return False

        old_process = self.processes[runner_instance_id]
        port = self.ports[runner_instance_id]

        if old_process.is_alive():
            logger.info(f"Restarting instance {runner_instance_id} on port {port}")
            old_process.terminate()
            old_process.join(timeout=5)

        # Create new process
        new_process = self._create_uvicorn_process(port, runner_instance_id)
        new_process.start()
        self.processes[runner_instance_id] = new_process

        logger.info(f"Instance {runner_instance_id} restarted (new PID: {new_process.pid})")
        return True

    def get_instance_status(self) -> Dict[int, Dict]:
        """
        Get status of all runner instances.

        Returns:
            Dict: Status information for each instance
        """
        status = {}
        for i, process in enumerate(self.processes):
            status[i] = {
                "port": self.ports[i],
                "pid": process.pid,
                "alive": process.is_alive(),
                "exitcode": process.exitcode if not process.is_alive() else None,
            }
        return status

    def monitor_instances(self, check_interval: int = 30) -> None:
        """
        Monitor instances and automatically restart failed ones.

        Args:
            check_interval: Seconds between health checks
        """
        logger.info(f"Starting instance monitoring (check interval: {check_interval}s)")

        try:
            while True:
                time.sleep(check_interval)

                for i, process in enumerate(self.processes):
                    if not process.is_alive():
                        logger.warning(f"Instance {i} on port {self.ports[i]} died, restarting...")
                        self.restart_instance(i)

        except KeyboardInterrupt:
            logger.info("Instance monitoring stopped")

    def wait_for_termination(self) -> None:
        """
        Wait for all processes to complete (typically until interrupted).
        """
        try:
            # Set up signal handlers for graceful shutdown
            def signal_handler(signum, frame):
                logger.info(f"Received signal {signum}, shutting down...")
                self.stop_all_instances()
                sys.exit(0)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            # Wait for all processes
            for process in self.processes:
                process.join()

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down...")
            self.stop_all_instances()
