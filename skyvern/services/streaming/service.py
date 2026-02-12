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


class StreamingService:
    """
    Service for capturing and streaming screenshots from active workflows/tasks.
    This service monitors running workflow runs via database queries and captures
    screenshots when a workflow run is active, uploading them to storage.
    """

    def __init__(self) -> None:
        self._monitoring_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start_monitoring(self) -> asyncio.Task:
        """
        Start the monitoring loop as a background task.

        Returns:
            The asyncio Task handling the monitoring loop.
        """
        if self._monitoring_task and not self._monitoring_task.done():
            LOG.warning("Streaming service monitoring is already running")
            return self._monitoring_task

        self._stop_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitor_loop())
        return self._monitoring_task

    async def stop_monitoring(self) -> None:
        """
        Stop the monitoring loop gracefully.
        """
        if not self._monitoring_task or self._monitoring_task.done():
            return

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
                    workflow_runs: list[WorkflowRun] = []

                    # Query running workflows for each organization
                    for org in organizations:
                        org_workflow_runs = await app.DATABASE.get_workflow_runs(
                            organization_id=org.organization_id,
                            status=[WorkflowRunStatus.running],
                        )
                        workflow_runs.extend(org_workflow_runs)
                except Exception as e:
                    LOG.debug("Failed to get running workflow runs", error=str(e))
                    continue

                # Skip if no running workflows found
                if not workflow_runs:
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
                            break

                    # Skip if no valid workflow run found
                    if not organization_id or not workflow_run_id or not file_name:
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

                # Capture screenshot
                await self._capture_screenshot(png_file_path)

                # Upload to storage
                try:
                    await app.STORAGE.save_streaming_file(organization_id, file_name)
                except Exception as e:
                    LOG.debug(
                        "Failed to upload screenshot",
                        organization_id=organization_id,
                        file_name=file_name,
                        error=str(e),
                    )

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
