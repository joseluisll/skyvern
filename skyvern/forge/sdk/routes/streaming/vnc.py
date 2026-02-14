"""
Provides WS endpoints for streaming a remote browser via VNC.

NOTE(jdo:streaming-local-dev)
-----------------------------
  - grep the above for local development seams
  - augment those seams as indicated, then
  - stand up https://github.com/jomido/whyvern

"""

import structlog
from urllib.parse import quote_plus

from fastapi import HTTPException, WebSocket
from websockets.exceptions import ConnectionClosedOK

from skyvern.config import settings
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import _auth as local_auth
from skyvern.forge.sdk.routes.streaming.auth import auth as real_auth
from skyvern.forge.sdk.routes.streaming.auth import get_x_api_key as streaming_get_x_api_key
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    Loops,
    VncChannel,
    get_vnc_channel_for_browser_session,
    get_vnc_channel_for_task,
    get_vnc_channel_for_workflow_run,
    build_vnc_url_from_session,
)
from skyvern.forge.sdk.utils.aio import collect
from skyvern.forge.sdk.routes.streaming.verify import verify_workflow_run
from skyvern.forge.sdk.services.org_auth_service import get_current_org

LOG = structlog.get_logger()


@base_router.websocket("/stream/vnc/browser_session/{browser_session_id}")
async def browser_session_stream(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, browser_session_id=browser_session_id, token=token)


@legacy_base_router.websocket("/stream/vnc/task/{task_id}")
async def task_stream(
    websocket: WebSocket,
    task_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, task_id=task_id, token=token)


@legacy_base_router.websocket("/stream/vnc/workflow_run/{workflow_run_id}")
async def workflow_run_stream(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, workflow_run_id=workflow_run_id, token=token)


async def stream(
    websocket: WebSocket,
    *,
    apikey: str | None = None,
    browser_session_id: str | None = None,
    client_id: str | None = None,
    task_id: str | None = None,
    token: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    if not client_id:
        LOG.error(
            "Client ID not provided for vnc stream.",
            browser_session_id=browser_session_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )
        return

    LOG.debug(
        "Starting vnc stream.",
        browser_session_id=browser_session_id,
        client_id=client_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )

    auth = local_auth if settings.ENV == "local" else real_auth
    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.info("Authentication failed.", task_id=task_id, workflow_run_id=workflow_run_id)
        return

    vnc_channel: VncChannel
    loops: Loops

    if browser_session_id:
        result = await get_vnc_channel_for_browser_session(
            client_id=client_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.debug(
                "No vnc context found for the browser session.",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result
    elif task_id:
        result = await get_vnc_channel_for_task(
            client_id=client_id,
            task_id=task_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.debug("No vnc context found for the task.", task_id=task_id, organization_id=organization_id)
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result

        LOG.info("Starting vnc for task.", task_id=task_id, organization_id=organization_id)

    elif workflow_run_id:
        LOG.info("Starting vnc for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        result = await get_vnc_channel_for_workflow_run(
            client_id=client_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.debug(
                "No vnc context found for the workflow run.",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result

        LOG.info("Starting vnc for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id)
    else:
        LOG.error("Neither task ID nor workflow run ID was provided.")
        return

    try:
        LOG.info(
            "Starting vnc loops.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except ConnectionClosedOK:
        LOG.info(
            "VNC connection closed cleanly.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.exception(
            "An exception occurred in the vnc loop.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the vnc session.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await vnc_channel.close(reason="vnc-closed")


async def _authorize_stream_request(apikey: str | None, token: str | None) -> str:
    if not apikey and not token:
        raise HTTPException(status_code=401, detail="Missing credentials")

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("Failed to authorize stream request", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to authenticate")

    if not organization or not organization.organization_id:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return organization.organization_id


@base_router.get("/stream/local/vnc/workflow_run/{workflow_run_id}")
async def local_direct_vnc_for_workflow(
    workflow_run_id: str,
    client_id: str | None = None,
    apikey: str | None = None,
    token: str | None = None,
) -> dict[str, object]:
    if settings.ENV != "local":
        raise HTTPException(status_code=403, detail="Local direct VNC only available in local env")

    organization_id = await _authorize_stream_request(apikey=apikey, token=token)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run or not browser_session:
        raise HTTPException(status_code=404, detail="Workflow run not available")

    vnc_url = build_vnc_url_from_session(browser_session, settings.SKYVERN_BROWSER_VNC_PORT)

    if not vnc_url:
        raise HTTPException(status_code=500, detail="Unable to determine VNC URL")

    if client_id:
        delimiter = "&" if "?" in vnc_url else "?"
        vnc_url = f"{vnc_url}{delimiter}client_id={quote_plus(client_id)}"

    if vnc_url.startswith("wss://"):
        x_api_key = await streaming_get_x_api_key(organization_id)
        if x_api_key:
            delimiter = "&" if "?" in vnc_url else "?"
            vnc_url = f"{vnc_url}{delimiter}x-api-key={quote_plus(x_api_key)}"

    return {
        "url": vnc_url,
    }
