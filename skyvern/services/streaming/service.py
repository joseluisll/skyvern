import asyncio
import os
import subprocess
from typing import Optional

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

INTERVAL = 1
LOG = structlog.get_logger(__name__)

# VNC Streaming Configuration
DISPLAY_NUM = ":99"
SCREEN_GEOMETRY = "1920x1080x24"
VNC_PORT = "5900"
WS_PORT = "6080"


class StreamingService:
    """
    Service for capturing and streaming screenshots from active workflows/tasks.
    This service monitors running workflow runs via database queries and captures
    screenshots when a workflow run is active, uploading them to storage.

    It also manages the VNC streaming infrastructure (Xvfb, x11vnc, websockify).
    """

    def __init__(self) -> None:
        self._monitoring_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._vnc_processes: dict[str, subprocess.Popen] = {}

    def _ensure_vnc_services_running(self) -> None:
        """
        Ensure VNC streaming services are running (Xvfb, x11vnc, websockify).
        These services are required for screenshot capture and streaming.
        """
        LOG.info("Ensuring VNC streaming services are running...")

        # Set DISPLAY environment variable
        os.environ["DISPLAY"] = DISPLAY_NUM

        def is_running_exact(process_name: str) -> bool:
            try:
                proc = subprocess.Popen(
                    ["pgrep", "-x", process_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                _, _ = proc.communicate()
                return proc.returncode == 0
            except Exception:
                return False

        def is_running_match(pattern: str) -> bool:
            try:
                proc = subprocess.Popen(
                    ["pgrep", "-f", pattern],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, _ = proc.communicate()
                return proc.returncode == 0 and bool(stdout.strip())
            except Exception:
                return False

        def start_xvfb() -> None:
            LOG.info("Starting Xvfb...")
            # Clean up any existing lock file
            lock_file = f"/tmp/.X{DISPLAY_NUM[1:]}-lock"
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                    LOG.debug("Removed existing X server lock file", lock_file=lock_file)
                except Exception as e:
                    LOG.warning("Failed to remove X server lock file", error=str(e))

            cmd = ["Xvfb", DISPLAY_NUM, "-screen", "0", SCREEN_GEOMETRY]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._vnc_processes["xvfb"] = proc
            LOG.info("Xvfb started successfully")

        def start_x11vnc() -> None:
            LOG.info("Starting x11vnc...")
            cmd = [
                "x11vnc",
                "-display",
                DISPLAY_NUM,
                "-bg",
                "-nopw",
                "-listen",
                "localhost",
                "-xkb",
                "-forever",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._vnc_processes["x11vnc"] = proc
            LOG.info("x11vnc started successfully")

        def start_websockify() -> None:
            LOG.info("Starting websockify...")
            cmd = [
                "websockify",
                WS_PORT,
                f"localhost:{VNC_PORT}",
                "--daemon",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._vnc_processes["websockify"] = proc
            LOG.info("websockify started successfully")

        # Check if VNC binaries are available
        vnc_available = True
        for binary in ["Xvfb", "x11vnc", "websockify"]:
            try:
                subprocess.run([binary, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.CalledProcessError, FileNotFoundError):
                LOG.error(f"VNC binary not available: {binary}")
                vnc_available = False

        if not vnc_available:
            LOG.error("One or more VNC services are not installed. Screenshot streaming will be disabled.")
            return

        # Start Xvfb if not running
        if not is_running_exact("Xvfb"):
            start_xvfb()
            LOG.info("Xvfb process restarted")
        else:
            LOG.debug("Xvfb already running")

        # Start x11vnc if not running
        if not is_running_exact("x11vnc"):
            start_x11vnc()
            LOG.info("x11vnc process restarted")
        else:
            LOG.debug("x11vnc already running")

        # Start websockify if not running
        if not is_running_match(f"websockify.*{WS_PORT}"):
            start_websockify()
            LOG.info("websockify process restarted")
        else:
            LOG.debug("websockify already running")

        LOG.info(
            "VNC streaming services configured",
            display=DISPLAY_NUM,
            vnc_port=VNC_PORT,
            ws_port=WS_PORT,
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

        # Ensure VNC services are running before starting monitoring
        self._ensure_vnc_services_running()

        self._stop_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitor_loop())
        return self._monitoring_task

    async def stop_monitoring(self) -> None:
        """
        Stop the monitoring loop gracefully.
        """
        if not self._monitoring_task or self._monitoring_task.done():
            return

        # Clean up VNC processes
        for name, proc in list(self._vnc_processes.items()):
            try:
                proc.terminate()
                LOG.info(f"Terminated {name} process")
            except Exception as e:
                LOG.warning(f"Failed to terminate {name} process", error=str(e))
        self._vnc_processes.clear()

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
                if await self._capture_screenshot(png_file_path):
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

    async def _capture_screenshot(self, file_path: str) -> bool:
        """
        Capture a screenshot using xwd/xwdtopnm/pnmtopng pipeline.

        Args:
            file_path: Path to save the screenshot PNG file.

        Returns:
            True if capture succeeded, False otherwise.
        """
        try:
            subprocess.run(
                f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {file_path}",
                shell=True,
                env={"DISPLAY": ":99"},
                check=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            LOG.error("Failed to capture screenshot", error=str(e))
            return False
        except Exception as e:
            LOG.error("Unexpected error capturing screenshot", error=str(e), exc_info=True)
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
        if not await self._capture_screenshot(png_file_path):
            return None

        # Upload to storage
        try:
            await app.STORAGE.save_streaming_file(organization_id, file_name)
            return file_name
        except Exception as e:
            LOG.error("Failed to upload screenshot", organization_id=organization_id, file_name=file_name, error=str(e))
            return None
