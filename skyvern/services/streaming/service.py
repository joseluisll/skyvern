import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.config import settings

INTERVAL = 10
LOG = structlog.get_logger(__name__)


@dataclass
class VncProcess:
    display_number: int
    vnc_port: int
    xvfb_process: subprocess.Popen
    x11vnc_process: subprocess.Popen
    websockify_process: subprocess.Popen


# VNC Streaming Configuration
BASE_DISPLAY_NUM = ":99"
BASE_DISPLAY = 100
BASE_VNC_PORT = 6000
SCREEN_GEOMETRY = "1920x1080x24"


class StreamingService:
    """
    Service for capturing and streaming screenshots from active workflows/tasks.
    This service monitors running workflow runs via database queries and captures
    screenshots when a workflow run is active, uploading them to storage.

    It also manages the VNC streaming infrastructure (Xvfb, x11vnc, websockify)
    with multi-session support for concurrent workflows.
    """

    def __init__(self) -> None:
        self._monitoring_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Multi-session VNC management
        self._session_vnc: dict[str, VncProcess] = {}
        self._used_displays: set[int] = set()
        self._used_ports: set[int] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    def _allocate_display(self) -> int:
        """
        Allocate a free display number.

        WARNING: This method must only be called while holding self._lock.
        It modifies shared state (self._used_displays) without internal synchronization.
        """
        display = BASE_DISPLAY
        while display in self._used_displays:
            display += 1
        self._used_displays.add(display)
        return display

    def _allocate_port(self) -> int:
        """
        Allocate a free VNC port.

        WARNING: This method must only be called while holding self._lock.
        It modifies shared state (self._used_ports) without internal synchronization.
        """
        port = BASE_VNC_PORT
        while port in self._used_ports:
            port += 1
        self._used_ports.add(port)
        return port

    async def start_vnc_for_session(
        self,
        session_id: str,
        organization_id: str,
    ) -> tuple[int, int]:
        """Start Xvfb, x11vnc, and websockify for a browser session.

        Args:
            session_id: The browser session ID
            organization_id: The organization ID

        Returns:
            tuple[int, int]: (display_number, vnc_port)
        """
        async with self._lock:
            display = self._allocate_display()
            vnc_port = self._allocate_port()
        rfb_port = 5900 + (display - BASE_DISPLAY)

        LOG.info(
            "Starting VNC for session",
            session_id=session_id,
            display=display,
            vnc_port=vnc_port,
            rfb_port=rfb_port,
        )

        # Start Xvfb
        xvfb = subprocess.Popen(
            [
                "Xvfb",
                f":{display}",
                "-screen",
                "0",
                f"{settings.BROWSER_WIDTH}x{settings.BROWSER_HEIGHT}x24",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)
        if xvfb.poll() is not None:
            LOG.error("Xvfb failed to start", display=display, returncode=xvfb.returncode)
            self._used_displays.discard(display)
            self._used_ports.discard(vnc_port)
            raise RuntimeError(f"Xvfb failed to start on display :{display}")

        # Start x11vnc
        x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                f":{display}",
                "-forever",
                "-shared",
                "-rfbport",
                str(rfb_port),
                "-xkb",
                "-nopw",
                "-noxdamage",
                "-noshm",
                "-noxfixes",
                "-noxrecord",
                "-listen",
                "0.0.0.0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.3)
        if x11vnc.poll() is not None:
            LOG.error("x11vnc failed to start", display=display, returncode=x11vnc.returncode)
            xvfb.terminate()
            self._used_displays.discard(display)
            self._used_ports.discard(vnc_port)
            raise RuntimeError(f"x11vnc failed to start on display :{display}")

        # Start websockify
        websockify = subprocess.Popen(
            [
                "websockify",
                "--web=/usr/share/novnc",
                str(vnc_port),
                f"localhost:{rfb_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.2)
        if websockify.poll() is not None:
            LOG.error("websockify failed to start", vnc_port=vnc_port, returncode=websockify.returncode)
            x11vnc.terminate()
            xvfb.terminate()
            self._used_displays.discard(display)
            self._used_ports.discard(vnc_port)
            raise RuntimeError(f"websockify failed to start on port {vnc_port}")

        self._session_vnc[session_id] = VncProcess(
            display_number=display,
            vnc_port=vnc_port,
            xvfb_process=xvfb,
            x11vnc_process=x11vnc,
            websockify_process=websockify,
        )

        LOG.info(
            "VNC started for session",
            session_id=session_id,
            display=display,
            vnc_port=vnc_port,
        )

        return display, vnc_port

    async def stop_vnc_for_session(
        self,
        session_id: str,
        organization_id: str | None = None,
    ) -> None:
        """Stop VNC processes for a browser session."""
        if session_id not in self._session_vnc:
            LOG.debug("No VNC instance found for session", session_id=session_id)
            return

        vnc = self._session_vnc[session_id]

        LOG.info(
            "Stopping VNC for session",
            session_id=session_id,
            display=vnc.display_number,
            vnc_port=vnc.vnc_port,
        )

        # Terminate processes in reverse order of startup
        for proc in [vnc.websockify_process, vnc.x11vnc_process, vnc.xvfb_process]:
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                LOG.exception("Error terminating VNC process", session_id=session_id)

        # Kill any orphaned processes by display/port
        await self._kill_orphaned_processes(vnc.display_number, vnc.vnc_port)

        # Release resources
        self._used_displays.discard(vnc.display_number)
        self._used_ports.discard(vnc.vnc_port)
        del self._session_vnc[session_id]

        # Update database (only if organization_id is provided)
        if organization_id:
            try:
                await app.DATABASE.update_persistent_browser_session(
                    browser_session_id=session_id,
                    organization_id=organization_id,
                    display_number=None,
                    vnc_port=None,
                )
            except Exception:
                LOG.exception("Failed to update database with VNC cleanup", session_id=session_id)

        LOG.info("VNC stopped for session", session_id=session_id)

    async def _kill_orphaned_processes(self, display_number: int, vnc_port: int) -> None:
        """Kill any orphaned VNC processes by display/port."""
        try:
            # Kill x11vnc processes using this display
            subprocess.run(
                ["pkill", "-f", f"x11vnc.*:{display_number}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Kill websockify processes using this port
            subprocess.run(
                ["pkill", "-f", f"websockify.* {vnc_port} "],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Kill Xvfb processes using this display
            subprocess.run(
                ["pkill", "-f", f"Xvfb :{display_number}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            LOG.exception("Error killing orphaned VNC processes")

    def get_display_for_session(self, session_id: str) -> int | None:
        """Get the display number for a session."""
        if session_id in self._session_vnc:
            return self._session_vnc[session_id].display_number
        return None

    async def _ensure_base_vnc_running(self) -> None:
        """
        Ensure base (:99) VNC display is running for backward compatibility.
        This is used as fallback for workflows without browser sessions.
        """
        LOG.info("Ensuring base VNC display :99 is running...")

        os.environ["DISPLAY"] = BASE_DISPLAY_NUM

        async def is_running_exact(process_name: str) -> bool:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep",
                    "-x",
                    process_name,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                await proc.wait()
                return proc.returncode == 0
            except Exception:
                return False

        async def is_running_match(pattern: str) -> bool:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep",
                    "-f",
                    pattern,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                return proc.returncode == 0 and bool(stdout.strip())
            except Exception:
                return False

        async def start_xvfb() -> None:
            LOG.info("Starting Xvfb...")
            # Clean up any existing lock file
            lock_file = f"/tmp/.X{BASE_DISPLAY_NUM[1:]}-lock"
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                    LOG.debug("Removed existing X server lock file", lock_file=lock_file)
                except Exception as e:
                    LOG.warning("Failed to remove X server lock file", error=str(e))

            proc = await asyncio.create_subprocess_exec(
                "Xvfb",
                BASE_DISPLAY_NUM,
                "-screen",
                "0",
                SCREEN_GEOMETRY,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOG.info("Xvfb started successfully")

        async def start_x11vnc() -> None:
            LOG.info("Starting x11vnc...")
            proc = await asyncio.create_subprocess_exec(
                "x11vnc",
                "-display",
                BASE_DISPLAY_NUM,
                "-bg",
                "-nopw",
                "-listen",
                "localhost",
                "-xkb",
                "-forever",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOG.info("x11vnc started successfully")

        async def start_websockify() -> None:
            LOG.info("Starting websockify...")
            proc = await asyncio.create_subprocess_exec(
                "websockify",
                "6080",
                f"localhost:5900",
                "--daemon",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOG.info("websockify started successfully")

        # Check if VNC binaries are available
        vnc_available = True
        for binary in ["Xvfb", "x11vnc", "websockify"]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary,
                    "--help",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await proc.wait()
            except (subprocess.CalledProcessError, FileNotFoundError):
                LOG.error(f"VNC binary not available: {binary}")
                vnc_available = False

        if not vnc_available:
            LOG.error("One or more VNC services are not installed. Screenshot streaming will be disabled.")
            return

        # Start Xvfb if not running
        if not await is_running_exact("Xvfb"):
            await start_xvfb()
            LOG.info("Xvfb process restarted")
        else:
            LOG.debug("Xvfb already running")

        # Start x11vnc if not running
        if not await is_running_exact("x11vnc"):
            await start_x11vnc()
            LOG.info("x11vnc process restarted")
        else:
            LOG.debug("x11vnc already running")

        # Start websockify if not running
        if not await is_running_match("websockify.*6080"):
            await start_websockify()
            LOG.info("websockify process restarted")
        else:
            LOG.debug("websockify already running")

        LOG.info(
            "Base VNC display configured",
            display=BASE_DISPLAY_NUM,
        )

    def start_monitoring(self) -> asyncio.Task:
        """
        Start the monitoring loop as a background task.

        Returns:
            The asyncio Task handling the monitoring loop.
        """
        if self._monitoring_task and not self._monitoring_task.done():
            LOG.warning("Streaming service monitoring is already running")
            return self._monitoring_task

        # Ensure base VNC services are running before starting monitoring
        # Fire and forget to avoid blocking startup
        asyncio.create_task(self._ensure_base_vnc_running())

        self._stop_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitor_loop())
        return self._monitoring_task

    async def stop_monitoring(self) -> None:
        """
        Stop the monitoring loop gracefully.
        """
        if not self._monitoring_task or self._monitoring_task.done():
            return

        # Clean up all VNC processes
        LOG.info("Stopping all VNC sessions", count=len(self._session_vnc))
        session_ids = list(self._session_vnc.keys())
        for session_id in session_ids:
            await self.stop_vnc_for_session(session_id, organization_id="")

        self._stop_event.set()
        try:
            await self._monitoring_task
        except asyncio.CancelledError:
            LOG.info("Streaming service monitoring stopped gracefully")
        finally:
            self._monitoring_task = None

    async def _monitor_loop(self) -> None:
        """
        Main monitoring loop that checks for active tasks/workflows and captures screenshots.
        """
        LOG.info("Starting streaming service monitoring loop")

        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(INTERVAL)

                # Check if we should stop
                if self._stop_event.is_set():
                    break

                # Get all organizations and check for running workflows in each
                try:
                    organizations = await app.DATABASE.get_all_organizations()
                    LOG.info("Discovered organizations", count=len(organizations))

                    workflow_runs: list[WorkflowRun] = []

                    # Query running workflows for each organization
                    for org in organizations:
                        org_workflow_runs = await app.DATABASE.get_workflow_runs(
                            organization_id=org.organization_id,
                            status=[WorkflowRunStatus.running],
                        )
                        workflow_runs.extend(org_workflow_runs)

                    LOG.info("Found running workflows", count=len(workflow_runs))
                except Exception as e:
                    LOG.debug("Failed to get running workflow runs", error=str(e))
                    continue

                # Skip if no running workflows found
                if not workflow_runs:
                    LOG.debug("No active workflow runs found - nothing to do")
                    continue

                # Get task/workflow info to check status
                try:
                    file_name: str | None = None
                    organization_id: str | None = None
                    workflow_run_id: str | None = None

                    # Use the first running workflow run (or iterate through all)
                    for workflow_run in workflow_runs:
                        if workflow_run and workflow_run.status == WorkflowRunStatus.running:
                            organization_id = workflow_run.organization_id
                            workflow_run_id = workflow_run.workflow_run_id
                            file_name = f"{workflow_run_id}.png"
                            LOG.debug("Selected workflow run for screenshot capture", workflow_run_id=workflow_run_id)
                            break

                    # Skip if no valid workflow run found
                    if not organization_id or not workflow_run_id or not file_name:
                        LOG.debug("No valid workflow run found - nothing to do")
                        continue

                except Exception as e:
                    LOG.exception(
                        "Failed to get workflow run while taking streaming screenshot in worker",
                        workflow_run_id=workflow_run_id,
                        organization_id=organization_id,
                        error=str(e),
                    )
                    continue

                # Ensure directory exists
                org_dir = f"{get_skyvern_temp_dir()}/{organization_id}"
                os.makedirs(org_dir, exist_ok=True)
                png_file_path = f"{org_dir}/{file_name}"

                # Capture and upload screenshot
                if await self._capture_screenshot(png_file_path, workflow_run_id, organization_id):
                    LOG.debug("Screenshot captured successfully", workflow_run_id=workflow_run_id)
                    try:
                        await app.STORAGE.save_streaming_file(organization_id, file_name)
                        LOG.debug(
                            "Screenshot uploaded to storage", workflow_run_id=workflow_run_id, file_name=file_name
                        )
                    except Exception as e:
                        LOG.debug(
                            "Failed to upload screenshot",
                            organization_id=organization_id,
                            workflow_run_id=workflow_run_id,
                            file_name=file_name,
                            error=str(e),
                        )
                else:
                    LOG.debug("Screenshot capture failed", workflow_run_id=workflow_run_id)

            except Exception as e:
                LOG.error("Unexpected error in streaming monitoring loop", error=str(e), exc_info=True)

        LOG.info("Streaming service monitoring loop stopped")

    async def _capture_screenshot(
        self,
        file_path: str,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
    ) -> bool:
        """
        Capture a screenshot using xwd/xwdtopnm/pnmtopng pipeline.

        Args:
            file_path: Path to save the screenshot PNG file.
            workflow_run_id: Optional workflow run ID to get display from.
            organization_id: Optional organization ID for session lookup.

        Returns:
            True if capture succeeded, False otherwise.
        """
        display = BASE_DISPLAY_NUM  # Default to :99

        if workflow_run_id and organization_id:
            # Get display from database
            try:
                browser_session = await app.DATABASE.get_persistent_browser_session_by_runnable_id(
                    workflow_run_id, organization_id
                )
                if browser_session and browser_session.display_number:
                    display = f":{browser_session.display_number}"
            except Exception:
                LOG.debug("Failed to get display from browser session, using default", display=display)

        try:
            subprocess.run(
                f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {file_path}",
                shell=True,
                env={"DISPLAY": display},
                check=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            LOG.error("Failed to capture screenshot", display=display, error=str(e))
            return False
        except Exception as e:
            LOG.error("Unexpected error capturing screenshot", display=display, error=str(e), exc_info=True)
            return False

    async def capture_screenshot_once(self, organization_id: str, identifier: str) -> Optional[str]:
        """
        Capture a single screenshot and upload it.

        Args:
            organization_id: The organization ID for storage.
            identifier: Task ID or workflow run ID to use as filename prefix.

        Returns:
            The file name if successful, None otherwise.
        """
        file_name = f"{identifier}.png"

        # Ensure directory exists
        org_dir = f"{get_skyvern_temp_dir()}/{organization_id}"
        os.makedirs(org_dir, exist_ok=True)
        png_file_path = f"{org_dir}/{file_name}"

        # Capture screenshot
        if not await self._capture_screenshot(png_file_path, identifier, organization_id):
            return None

        # Upload to storage
        try:
            await app.STORAGE.save_streaming_file(organization_id, file_name)
            return file_name
        except Exception as e:
            LOG.error("Failed to upload screenshot", organization_id=organization_id, file_name=file_name, error=str(e))
            return None
