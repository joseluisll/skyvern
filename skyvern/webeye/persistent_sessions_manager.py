"""Protocol definition for PersistentSessionsManager implementations."""

from __future__ import annotations

from typing import Protocol

from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserType,
)
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_state import BrowserState


class PersistentSessionsManager(Protocol):
    """Protocol defining the interface for persistent browser session management."""

    def watch_session_pool(self) -> None:
        """Initialize monitoring of the session pool."""
        ...

    async def begin_session(
        self,
        *,
        browser_session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Begin a browser session for a specific runnable."""
        ...

    async def get_browser_address(self, session_id: str, organization_id: str) -> str:
        """Get the browser address for a session."""
        ...

    async def get_session_by_runnable_id(
        self, runnable_id: str, organization_id: str
    ) -> PersistentBrowserSession | None:
        """Get a browser session by runnable ID."""
        ...

    async def get_active_sessions(self, organization_id: str) -> list[PersistentBrowserSession]:
        """Get all active sessions for an organization."""
        ...

    async def get_browser_state(self, session_id: str, organization_id: str | None = None) -> BrowserState | None:
        """Get the browser state for a session."""
        ...

    async def set_browser_state(self, session_id: str, browser_state: BrowserState) -> None:
        """Set the browser state for a session."""
        ...

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        """Get a browser session by session ID."""
        ...

    async def create_session(
        self,
        organization_id: str,
        proxy_location: ProxyLocationInput | None = ProxyLocation.RESIDENTIAL,
        url: str | None = None,
        runnable_id: str | None = None,
        runnable_type: str | None = None,
        timeout_minutes: int | None = None,
        extensions: list[Extensions] | None = None,
        browser_type: PersistentBrowserType | None = None,
        is_high_priority: bool = False,
    ) -> PersistentBrowserSession:
        """Create a new browser session for an organization and return its ID with the browser state."""

        LOG.info(
            "Creating new browser session",
            organization_id=organization_id,
        )

        browser_session_db = await self.database.create_persistent_browser_session(
            organization_id=organization_id,
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            timeout_minutes=timeout_minutes,
            proxy_location=proxy_location,
            extensions=extensions,
        )

        # Start VNC for this session via StreamingService
        display, vnc_port = await app.STREAMING_SERVICE.start_vnc_for_session(
            session_id=browser_session_db.persistent_browser_session_id
        )

        # Update the session with VNC info
        browser_session_db = await self.database.update_persistent_browser_session(
            browser_session_db.persistent_browser_session_id,
            organization_id=organization_id,
            display_number=display,
            vnc_port=vnc_port,
        )

        return browser_session_db

    async def occupy_browser_session(
        self,
        session_id: str,
        runnable_type: str,
        runnable_id: str,
        organization_id: str,
    ) -> None:
        """Occupy a browser session for use."""
        ...

    async def renew_or_close_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession:
        """Renew a session or close it if renewal fails."""
        ...

    async def update_status(
        self, session_id: str, organization_id: str, status: str
    ) -> PersistentBrowserSession | None:
        """Update the status of a browser session."""
        ...

    async def release_browser_session(self, session_id: str, organization_id: str) -> None:
        """Release a browser session."""
        ...

    async def close_session(self, organization_id: str, browser_session_id: str) -> None:
        """Close a specific browser session."""
        # Stop VNC for this session via StreamingService
        await app.STREAMING_SERVICE.stop_vnc_for_session(browser_session_id)

        browser_session = self._browser_sessions.get(browser_session_id)
        if browser_session:
            LOG.info(
                "Closing browser session",
                organization_id=organization_id,
                session_id=browser_session_id,
            )

            # Export session profile before closing (so it can be used to create browser profiles)
            browser_artifacts = browser_session.browser_state.browser_artifacts
            if browser_artifacts and browser_artifacts.browser_session_dir:
                try:
                    await app.STORAGE.store_browser_profile(
                        organization_id=organization_id,
                        profile_id=browser_session_id,
                        directory=browser_artifacts.browser_session_dir,
                    )
                    LOG.info(
                        "Exported browser session profile",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
                    )
                except Exception:
                    LOG.exception(
                        "Failed to export browser session profile",
                        browser_session_id=browser_session_id,
                        organization_id=organization_id,
                    )

            self._browser_sessions.pop(browser_session_id, None)

            try:
                await browser_session.browser_state.close()
            except TargetClosedError:
                LOG.info(
                    "Browser context already closed",
                    organization_id=organization_id,
                    session_id=browser_session_id,
                )
            except Exception:
                LOG.warning(
                    "Error while closing browser session",
                    organization_id=organization_id,
                    session_id=browser_session_id,
                    exc_info=True,
                )
        else:
            LOG.info(
                "Browser session not found in memory, marking as deleted in database",
                organization_id=organization_id,
                session_id=browser_session_id,
            )

        await self.database.close_persistent_browser_session(browser_session_id, organization_id)

    async def close_all_sessions(self, organization_id: str) -> None:
        """Close all browser sessions for an organization."""
        ...

    @classmethod
    async def close(cls) -> None:
        """Close all browser sessions across all organizations."""
        ...
