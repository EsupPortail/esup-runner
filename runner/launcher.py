# runner/launcher.py
"""
Simplified launcher for multi-instance Runner deployment.
Uses UvicornProcessManager for process management.
"""

import os
import sys

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.managers.process_manager import UvicornProcessManager

# Configure logging
logger = setup_default_logging()


def main():
    """
    Main entry point for multi-instance runner launcher.
    """
    # Create process manager
    manager = UvicornProcessManager(
        base_port=config.RUNNER_BASE_PORT, instances=config.RUNNER_INSTANCES
    )

    try:
        # Start all instances
        print(f"🚀 Starting {config.RUNNER_INSTANCES} runner instances...")
        manager.start_all_instances()

        # Display status
        status = manager.get_instance_status()
        print("\n📊 Instance Status:")
        print("-" * 50)
        for runner_instance_id, info in status.items():
            status_icon = "🟢" if info["alive"] else "🔴"
            print(
                f"{status_icon} Instance {runner_instance_id}: port {info['port']}, PID {info['pid']}"
            )

        print("\n✅ All instances started successfully!")
        print("   Press Ctrl+C to stop all instances")

        # Start monitoring if requested
        if config.RUNNER_MONITORING:
            print("🔍 Monitoring enabled - instances will be automatically restarted if they fail")
            manager.monitor_instances()
        else:
            # Wait for termination
            manager.wait_for_termination()

    except KeyboardInterrupt:
        print("\n⏹️  Shutting down all instances...")
        manager.stop_all_instances()
        print("✅ All instances stopped")
    except Exception as e:
        print(f"❌ Launcher error: {e}")
        manager.stop_all_instances()
        sys.exit(1)


def run_dev():
    """Run a single runner instance with Uvicorn reload (development mode)."""
    import uvicorn

    host = os.getenv("RUNNER_HOST", "0.0.0.0")
    port = int(os.getenv("RUNNER_PORT", str(config.RUNNER_BASE_PORT)))

    # Ensure instance-specific env vars are set before app import.
    os.environ.setdefault("RUNNER_INSTANCE_ID", "0")
    os.environ.setdefault("RUNNER_PORT", str(port))

    print(f"[DEV] Starting single runner instance on {host}:{port}")
    print(f"Manager URL: {config.MANAGER_URL}")

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=True,
        access_log=True,
        workers=1,
    )


if __name__ == "__main__":
    main()
