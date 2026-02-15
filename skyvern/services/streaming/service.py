from __future__ import annotations

import asyncio
import os
import subprocess
from typing import TYPE_CHECKING

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.services.streaming.vnc_manager import VncManager
from skyvern.utils.files import get_json_from_file, get_skyvern_state_file_path, initialize_skyvern_state_file

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.agent_db import AgentDB

LOG = structlog.get_logger()

SCREENSHOT_INTERVAL = 1  # seconds


class StreamingService:
    """Service for managing VNC and screenshot streaming.

    This service handles:
    - Base Xvfb display initialization
    - Per-session VNC management (via VncManager)
    - Screenshot streaming for live view
    """

    _instance: StreamingService | None = None
    _base_xvfb_process: subprocess.Popen | None = None
    _screenshot_task: asyncio.Task | None = None
    _running: bool = False
    _database: AgentDB | None = None

    def __new__(cls, database: AgentDB | None = None) -> StreamingService:
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._database = database
            cls._instance = instance
        elif database is not None:
            cls._instance._database = database
        return cls._instance

    @property
    def database(self) -> AgentDB:
        if self._database is None:
            raise RuntimeError("StreamingService database not initialized")
        return self._database

    async def start(self) -> None:
        """Start streaming services (called on app startup)."""
        if self._running:
            LOG.warning("StreamingService already running")
            return

        LOG.info("Starting StreamingService")

        # Start base Xvfb display
        await self._start_base_display()

        # Initialize state file for screenshot streaming
        await initialize_skyvern_state_file(task_id=None, workflow_run_id=None, organization_id=None)

        # Start screenshot streaming loop
        self._screenshot_task = asyncio.create_task(self._screenshot_loop())
        self._running = True

        LOG.info("StreamingService started")

    async def stop(self) -> None:
        """Stop streaming services (called on app shutdown)."""
        if not self._running:
            LOG.warning("StreamingService not running")
            return

        LOG.info("Stopping StreamingService")

        # Stop screenshot streaming
        if self._screenshot_task:
            self._screenshot_task.cancel()
            try:
                await self._screenshot_task
            except asyncio.CancelledError:
                pass
            self._screenshot_task = None

        # Stop all VNC instances
        await VncManager.stop_all()

        # Stop base Xvfb
        await self._stop_base_display()

        self._running = False
        LOG.info("StreamingService stopped")

    async def _start_base_display(self) -> None:
        """Start the base Xvfb display (:99)."""
        display_num = os.environ.get("SKYVERN_DEFAULT_DISPLAY", ":99")

        # Check if Xvfb is already running
        result = subprocess.run(["pgrep", "-x", "Xvfb"], capture_output=True)
        if result.returncode == 0:
            LOG.info("Xvfb already running")
            os.environ["DISPLAY"] = display_num
            return

        # Remove stale lock file if exists
        lock_file = f"/tmp/.X{display_num.lstrip(':')}-lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)

        screen_width = settings.BROWSER_WIDTH
        screen_height = settings.BROWSER_HEIGHT
        screen_geometry = f"{screen_width}x{screen_height}x24"

        LOG.info("Starting base Xvfb display", display=display_num, geometry=screen_geometry)

        self._base_xvfb_process = subprocess.Popen(
            ["Xvfb", display_num, "-screen", "0", screen_geometry],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        await asyncio.sleep(0.5)
        if self._base_xvfb_process.poll() is not None:
            LOG.error("Base Xvfb failed to start", returncode=self._base_xvfb_process.returncode)
            raise RuntimeError(f"Base Xvfb failed to start on display {display_num}")

        os.environ["DISPLAY"] = display_num
        LOG.info("Base Xvfb display started", display=display_num)

    async def _stop_base_display(self) -> None:
        """Stop the base Xvfb display."""
        if self._base_xvfb_process:
            LOG.info("Stopping base Xvfb display")
            try:
                self._base_xvfb_process.terminate()
                self._base_xvfb_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._base_xvfb_process.kill()
            except Exception:
                LOG.exception("Error stopping base Xvfb")
            self._base_xvfb_process = None

    async def _screenshot_loop(self) -> None:
        """Background task for taking screenshots of running tasks/workflows."""
        LOG.info("Screenshot streaming loop started")

        while True:
            await asyncio.sleep(SCREENSHOT_INTERVAL)

            try:
                current_json = get_json_from_file(get_skyvern_state_file_path())
            except Exception:
                continue

            task_id = current_json.get("task_id")
            workflow_run_id = current_json.get("workflow_run_id")
            organization_id = current_json.get("organization_id")

            if not organization_id or (not task_id and not workflow_run_id):
                continue

            try:
                file_name = await self._get_screenshot_filename(task_id, workflow_run_id, organization_id)
                if not file_name:
                    continue
            except Exception:
                LOG.exception(
                    "Failed to get task or workflow run while taking streaming screenshot",
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                continue

            # Get the display number for this session
            display = await self._get_display_for_runnable(task_id, workflow_run_id, organization_id)

            # Take screenshot
            await self._take_screenshot(organization_id, file_name, display)

    async def _get_screenshot_filename(
        self, task_id: str | None, workflow_run_id: str | None, organization_id: str
    ) -> str | None:
        """Get the filename for the screenshot based on task or workflow run status."""
        if workflow_run_id:
            workflow_run = await self.database.get_workflow_run(workflow_run_id=workflow_run_id)
            if not workflow_run or workflow_run.status in [
                WorkflowRunStatus.completed,
                WorkflowRunStatus.failed,
                WorkflowRunStatus.terminated,
            ]:
                return None
            return f"{workflow_run_id}.png"

        elif task_id:
            task = await self.database.get_task(task_id=task_id, organization_id=organization_id)
            if not task or task.status.is_final():
                return None
            return f"{task_id}.png"

        return None

    async def _get_display_for_runnable(
        self, task_id: str | None, workflow_run_id: str | None, organization_id: str
    ) -> str:
        """Get the display number for a task or workflow run."""
        default_display = os.environ.get("SKYVERN_DEFAULT_DISPLAY", ":99")

        try:
            browser_session = None
            if workflow_run_id:
                browser_session = await self.database.get_persistent_browser_session_by_runnable_id(
                    workflow_run_id, organization_id
                )
            elif task_id:
                browser_session = await self.database.get_persistent_browser_session_by_runnable_id(
                    task_id, organization_id
                )

            if browser_session and browser_session.display_number:
                return f":{browser_session.display_number}"
        except Exception:
            LOG.debug("Failed to get display number for session, using default", display=default_display)

        return default_display

    async def _take_screenshot(self, organization_id: str, file_name: str, display: str) -> None:
        """Take a screenshot and save it."""
        from skyvern.forge import app  # noqa: PLC0415

        # Create directory if needed
        os.makedirs(f"{get_skyvern_temp_dir()}/{organization_id}", exist_ok=True)
        png_file_path = f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}"

        # Take screenshot using xwd
        subprocess.run(
            f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {png_file_path}",
            shell=True,
            env={"DISPLAY": display},
        )

        try:
            await app.STORAGE.save_streaming_file(organization_id, file_name)
        except Exception:
            LOG.debug("Failed to upload screenshot", organization_id=organization_id, file_name=file_name)

    # VNC session management methods (delegate to VncManager)

    async def start_vnc_for_session(self, session_id: str) -> tuple[int, int]:
        """Start VNC for a browser session.

        Returns:
            tuple[int, int]: (display_number, vnc_port)
        """
        return await VncManager.start_vnc_for_session(session_id)

    async def stop_vnc_for_session(self, session_id: str) -> None:
        """Stop VNC for a browser session."""
        await VncManager.stop_vnc_for_session(session_id)

    def get_display_for_session(self, session_id: str) -> int | None:
        """Get the display number for a session."""
        return VncManager.get_display_for_session(session_id)

    @classmethod
    async def close(cls) -> None:
        """Close the streaming service (class method for compatibility)."""
        if cls._instance:
            await cls._instance.stop()
