# Persistent Browser Sessions (PBS) Integration with WorkflowRunV2

## Executive Summary

This document analyzes the integration between PersistentBrowserSessions (PBS), StreamingService, and WorkflowRunV2 in Skyvern. It explains how workflow runs reuse existing browser sessions, how debug sessions manage PBS lifecycle, and the sequence of operations when a workflow run is created with an existing PBS ID.

## Key Components

### 1. PersistentBrowserSession (PBS)
- **Purpose**: Maintains browser state across multiple workflow steps/runs
- **Key Fields** (in `PersistentBrowserSessionModel`):
  - `persistent_browser_session_id`: Unique identifier
  - `organization_id`: Owning organization
  - `display_number`: Xvfb display number for VNC streaming
  - `vnc_port`: Port for VNC connection
  - `status`: Session status (active, expired, etc.)
  - `timeout_at`: When session expires
  - `occupied_by_runnable_type`: "task_run" or "workflow_run"
  - `occupied_by_runnable_id`: ID of the run using this session

### 2. WorkflowRunV2
- **Purpose**: Executes workflow definitions with optional PBS reuse
- **Key Fields** (in `WorkflowRunModel`):
  - `workflow_run_id`: Unique identifier
  - `browser_session_id`: Optional link to PBS
  - `debug_session_id`: Link to debug session (if applicable)
  - Various execution state fields

### 3. DebugSession
- **Purpose**: Provides long-lived PBS sessions for interactive debugging
- **Key Fields** (in `DebugSessionModel`):
  - `debug_session_id`: Unique identifier
  - `browser_session_id`: Linked PBS session
  - `workflow_permanent_id`: Workflow being debugged
  - `timeout_at`: 4-hour expiration from creation

### 4. StreamingService
- **Purpose**: Manages VNC streaming for browser sessions
- **Integration**: Automatically started when PBS is created
- **Provides**: Live viewing capability via display number and VNC port

## Integration Flow: Workflow Run with Existing PBS ID

### Sequence Diagram

```
1. User creates workflow run with existing browser_session_id
   └─> POST /workflows/{workflow_permanent_id}/run
       {"browser_session_id": "bss_..."}

2. WorkflowService.create_workflow_run()
   └─> Validates PBS exists via DATABASE.get_persistent_browser_session()
       ├─> Returns error if not found
       └─> Creates WorkflowRun with browser_session_id
           └─> DATABASE.create_workflow_run() stores the link

3. WorkflowService.execute_workflow()
   └─> Calls PERSISTENT_SESSIONS_MANAGER.begin_session()
       ├─> Marks PBS as occupied by workflow run
       │   └─> Updates: occupied_by_runnable_type="workflow_run"
       │       occupied_by_runnable_id=workflow_run_id
       └─> Returns browser state for execution

4. Workflow execution proceeds
   └─> Uses the shared browser instance from PBS

5. Workflow completion/cleanup
   └─> clean_up_workflow() called
       ├─> BROWSER_MANAGER.cleanup_for_workflow_run()
       │   └─> PERSISTENT_SESSIONS_MANAGER.release_browser_session()
       │       └─> Clears occupied_by_* fields
       └─> Browser state may be persisted if workflow.persist_browser_session=true
```

### Detailed Steps

#### Step 1: Workflow Run Creation

**File**: `skyvern/forge/sdk/workflow/service.py:2371-2419` (`create_workflow_run`)

```python
async def create_workflow_run(
    self,
    workflow_request: WorkflowRequestBody,
    ...
) -> WorkflowRun:
    # Validate browser session exists if provided
    if workflow_request.browser_session_id:
        browser_session = await app.DATABASE.get_persistent_browser_session(
            session_id=workflow_request.browser_session_id,
            organization_id=organization_id,
        )
        if not browser_session:
            raise BrowserSessionNotFound(browser_session_id=workflow_request.browser_session_id)
    
    # Create workflow run with the browser_session_id
    return await app.DATABASE.create_workflow_run(
        ...
        browser_session_id=workflow_request.browser_session_id,
        ...
    )
```

**Key Points**:
- Validation ensures PBS exists before creating workflow run
- WorkflowRun is created with `browser_session_id` field populated
- No automatic PBS creation if ID is provided (user must manage lifecycle)

#### Step 2: Workflow Execution Setup

**File**: `skyvern/forge/sdk/workflow/service.py:746-850` (`execute_workflow`)

```python
async def execute_workflow(
    self,
    workflow_run_id: str,
    ...
) -> WorkflowRun:
    workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
    
    # Determine if we need to close browser on completion
    close_browser_on_completion = browser_session_id is None and not workflow_run.browser_address
    
    # Mark workflow as running
    workflow_run = await self.mark_workflow_run_as_running(workflow_run_id=workflow_run_id)
    
    # Auto-create PBS if needed (only if no browser_session_id provided)
    browser_session = None
    if not browser_profile_id:
        browser_session = await self.auto_create_browser_session_if_needed(
            organization.organization_id,
            workflow,
            browser_session_id=browser_session_id,  # If provided, returns None
            proxy_location=workflow_run.proxy_location,
        )
    
    if browser_session:
        browser_session_id = browser_session.persistent_browser_session_id
        close_browser_on_completion = True
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            browser_session_id=browser_session_id,
        )
    
    # Mark PBS as occupied by this workflow run
    if browser_session_id:
        try:
            await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
                browser_session_id=browser_session_id,
                runnable_type="workflow_run",
                runnable_id=workflow_run_id,
                organization_id=organization.organization_id,
            )
        except Exception:
            # Handle failure and mark workflow as failed
```

**Key Points**:
- `auto_create_browser_session_if_needed()` returns `None` if `browser_session_id` is already provided
- If PBS was auto-created, it updates the workflow run with new `browser_session_id`
- `begin_session()` marks the PBS as occupied by this specific workflow run
- This prevents other runs from using the same session concurrently

#### Step 3: Workflow Execution

**File**: `skyvern/forge/sdk/workflow/service.py:885-1299` (`_execute_workflow_blocks`)

The workflow execution uses the browser state from PBS:
- Browser state is retrieved via `PERSISTENT_SESSIONS_MANAGER.get_browser_state()`
- All blocks execute within this shared browser context
- VNC streaming continues throughout execution (if enabled)

#### Step 4: Cleanup and Session Release

**File**: `skyvern/forge/sdk/workflow/service.py:3024-3061` (`clean_up_workflow`)

```python
async def clean_up_workflow(
    self,
    workflow: Workflow,
    workflow_run: WorkflowRun,
    ...
    browser_session_id: str | None = None,
) -> None:
    # Determine if we should close the browser
    close_browser_on_completion = (
        close_browser_on_completion and 
        browser_session_id is None and 
        not workflow_run.browser_address
    )
    
    # Clean up browser state
    browser_state = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
        workflow_run.workflow_run_id,
        all_workflow_task_ids,
        close_browser_on_completion=close_browser_on_completion,
        browser_session_id=browser_session_id,
        organization_id=workflow_run.organization_id,
    )
```

**File**: `skyvern/webeye/real_browser_manager.py:356-400` (`cleanup_for_workflow_run`)

```python
async def cleanup_for_workflow_run(
    self,
    workflow_run_id: str,
    task_ids: list[str],
    close_browser_on_completion: bool = True,
    browser_session_id: str | None = None,
    organization_id: str | None = None,
) -> BrowserState | None:
    # Close browser state for workflow run
    browser_state_to_close = self.pages.pop(workflow_run_id, None)
    if browser_state_to_close:
        await browser_state_to_close.close(close_browser_on_completion=close_browser_on_completion)
    
    # Close browser states for all tasks in this workflow run
    for task_id in task_ids:
        task_browser_state = self.pages.pop(task_id, None)
        if task_browser_state:
            try:
                await task_browser_state.close(close_browser_on_completion=close_browser_on_completion)
            except Exception:
                # Log error but continue
    
    # Release the PBS if it was used
    if browser_session_id:
        if organization_id:
            await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                browser_session_id, organization_id=organization_id
            )
```

**Key Points**:
- Closes all task-level browser states
- Releases PBS by clearing `occupied_by_*` fields
- PBS remains active for potential reuse by other workflows
- If `workflow.persist_browser_session=true`, browser artifacts are saved

## Debug Session Integration

### Purpose
Debug sessions provide long-lived PBS sessions for interactive debugging with a 4-hour timeout.

### Flow

```
1. User creates debug session for a workflow
   └─> POST /debug-session/{workflow_permanent_id}

2. DebugSessionService.create_debug_session()
   └─> Checks if existing debug session exists
       ├─> If yes, renews timeout (adds 4 hours)
       │   └─> DATABASE.update_debug_session_timeout()
       └─> If no, creates new debug session
           ├─> Creates new PBS via PERSISTENT_SESSIONS_MANAGER.create_session()
           │   └─> StreamingService automatically starts VNC streaming
           └─> Creates DebugSession linking to PBS and workflow
               └─> DATABASE.create_debug_session()

3. User connects to debug session
   └─> Uses VNC display_number and vnc_port from PBS

4. Workflow execution with debug session
   └─> When creating workflow run, includes debug_session_id
       ├─> WorkflowRun created with debug_session_id field
       └─> During execution, uses the linked PBS

5. Debug session cleanup
   └─> When debug session expires or is deleted
       └─> PBS is automatically closed/removed
```

### Key Files

- **Debug Session Routes**: `skyvern/forge/sdk/routes/debug_sessions.py` (lines 1-200)
- **Debug Session Service**: `skyvern/services/debug_session_service.py` (lines 50-150)
- **Database Methods**: `skyvern/forge/sdk/db/agent_db.py` (lines 5370-5515)

## Technical Decisions and Design Patterns

### 1. Session Occupancy Pattern
**Decision**: PBS uses `occupied_by_runnable_type` and `occupied_by_runnable_id` fields to track usage.

**Rationale**:
- Prevents concurrent access to the same browser session
- Allows tracking which run is using a session for debugging/logging
- Enables proper cleanup when runs complete
- Supports both task_run and workflow_run types

### 2. Automatic VNC Streaming
**Decision**: VNC streaming starts automatically when PBS is created.

**Rationale**:
- Debug sessions require live viewing capability
- Consistent experience for all PBS users
- Minimal overhead (Xvfb + x11vnc are lightweight)
- Can be disabled if needed via configuration

### 3. Session Lifecycle Management
**Decision**: Separate begin/release pattern instead of automatic cleanup.

**Rationale**:
- Explicit control over when sessions are released
- Better error handling and recovery
- Allows for manual session management in edge cases
- Supports long-running debug sessions

### 4. Debug Session Timeout
**Decision**: 4-hour timeout for debug sessions.

**Rationale**:
- Long enough for debugging sessions
- Short enough to prevent resource leaks
- Renewable on each use (prevents interruption during active debugging)
- Balances user experience with system resources

## Edge Cases and Error Handling

### 1. Expired PBS
If a workflow run specifies an expired PBS ID:
- `create_workflow_run()` validates the session exists but not expiration
- During `execute_workflow()`, `begin_session()` may fail if session is expired
- Workflow is marked as failed with appropriate error message

### 2. Concurrent Access Attempt
If two workflows try to use the same PBS:
- Second `begin_session()` call will fail or wait depending on implementation
- Currently not explicitly handled (may cause race conditions)
- **Potential Improvement**: Add explicit locking mechanism

### 3. Debug Session with Expired PBS
If debug session's linked PBS expires:
- Next access attempt fails
- Debug session should be automatically renewed/created during workflow execution
- **Current Behavior**: Not automatically handled (user must recreate)
- **Potential Improvement**: Auto-recreate PBS when accessing expired debug session

### 4. Workflow Completion Without Cleanup
If workflow completes abruptly:
- Browser state may not be properly closed
- PBS remains marked as occupied
- **Current Behavior**: Eventually times out and becomes available again
- **Potential Improvement**: Add heartbeat mechanism to detect stale occupations

## Testing Strategy

### Existing Tests
1. **Streaming Integration Test** (`tests/unit_tests/test_forge_app_streaming_integration.py`)
   - Verifies StreamingService is properly initialized in ForgeApp

2. **Streaming Service Tests** (`tests/unit_tests/test_streaming_service.py`)
   - Tests screenshot capture, monitoring loop, etc.

3. **Persistent Browser Test Script** (`scripts/test_persistent_browsers.py`)
   - CLI tool for manual testing of PBS functionality
   - Includes commands to:
     - List active sessions
     - Create new sessions
     - Create workflow runs with specific browser_session_id
     - Close sessions

### Recommended Additional Tests

1. **Unit Test: Workflow Run with Existing PBS**
```python
async def test_workflow_run_with_existing_pbs():
    # Setup: Create a PBS
    pbs = await PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id="org_123",
        timeout_minutes=60,
    )
    
    # Create workflow run with the PBS ID
    workflow_run = await WorkflowService().create_workflow_run(
        workflow_request=WorkflowRequestBody(browser_session_id=pbs.persistent_browser_session_id),
        ...
    )
    
    # Verify: WorkflowRun has correct browser_session_id
    assert workflow_run.browser_session_id == pbs.persistent_browser_session_id
    
    # Verify: PBS is marked as occupied
    occupied_pbs = await DATABASE.get_persistent_browser_session(
        session_id=pbs.persistent_browser_session_id,
        organization_id="org_123",
    )
    assert occupied_pbs.occupied_by_runnable_type == "workflow_run"
    assert occupied_pbs.occupied_by_runnable_id == workflow_run.workflow_run_id
```

2. **Integration Test: Debug Session Lifecycle**
```python
async def test_debug_session_creates_pbs():
    # Create debug session
    debug_session = await DebugSessionService().create_debug_session(
        workflow_permanent_id="wpid_123",
        organization_id="org_456",
    )
    
    # Verify: PBS was created
    pbs = await DATABASE.get_persistent_browser_session(
        session_id=debug_session.browser_session_id,
        organization_id="org_456",
    )
    assert pbs is not None
    assert pbs.display_number is not None
    assert pbs.vnc_port is not None
    
    # Verify: StreamingService is running for this session
    # (Check that display and VNC port are accessible)
```

3. **Cleanup Test**
```python
async def test_workflow_cleanup_releases_pbs():
    # Setup: Create PBS and workflow run
    pbs = await PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id="org_123",
        timeout_minutes=60,
    )
    
    workflow_run = await WorkflowService().create_workflow_run(
        workflow_request=WorkflowRequestBody(browser_session_id=pbs.persistent_browser_session_id),
        ...
    )
    
    # Execute workflow
    await WorkflowService().execute_workflow(workflow_run.workflow_run_id, ...)
    
    # Verify: PBS is released after cleanup
    occupied_pbs = await DATABASE.get_persistent_browser_session(
        session_id=pbs.persistent_browser_session_id,
        organization_id="org_123",
    )
    assert occupied_pbs.occupied_by_runnable_type is None
    assert occupied_pbs.occupied_by_runnable_id is None
```

## Performance Considerations

### 1. Session Reuse Benefits
- **Reduced Browser Startup Time**: No need to launch browser for each workflow run
- **Preserved State**: Maintains cookies, localStorage, session across runs
- **Resource Efficiency**: Fewer browser processes running concurrently

### 2. Potential Bottlenecks
- **Concurrent Access**: Multiple workflows trying to use same PBS
  - *Mitigation*: Occupancy tracking prevents this
- **Memory Leaks**: Long-running sessions accumulating memory
  - *Mitigation*: Timeout-based cleanup, manual session management
- **VNC Streaming Overhead**: Continuous screenshot capture
  - *Mitigation*: Configurable streaming rate, optional for non-debug sessions

### 3. Optimization Opportunities
1. **Lazy VNC Streaming**: Only start when debug session is active
2. **Session Pooling**: Maintain pool of ready-to-use PBS instances
3. **Smart Cleanup**: Detect and cleanup stale occupied sessions
4. **Resource Monitoring**: Track memory usage and auto-scale

## Security Considerations

### 1. Session Isolation
- Each organization has separate PBS instances
- No cross-organization access to browser sessions
- VNC ports are organization-specific

### 2. Access Control
- PBS can only be accessed by workflows/runs in the same organization
- Debug sessions require proper authentication
- Browser session IDs are organization-scoped

### 3. Potential Risks
1. **Session Hijacking**: If browser_session_id is leaked
   - *Mitigation*: Use short-lived session IDs, validate organization ownership
2. **VNC Exposure**: VNC ports accessible if firewall misconfigured
   - *Mitigation*: Bind to localhost only, use authentication
3. **Persistent State Leaks**: Sensitive data remaining in browser
   - *Mitigation*: Clear sensitive data after workflow completion, profile-based isolation

## Documentation Gaps and Recommendations

### Current Documentation Status
- **Code Comments**: Minimal, mostly type hints
- **API Docs**: Endpoints exist but lack detailed examples
- **Architecture Diagrams**: None found
- **User Guides**: No specific documentation on PBS + workflow integration

### Recommended Documentation

1. **API Documentation Updates**
   - Add example requests for creating workflow runs with browser_session_id
   - Document debug session flow and VNC connection details
   - Add error response examples (e.g., BrowserSessionNotFound)

2. **Architecture Diagram**
   - Visual representation of PBS, WorkflowRun, DebugSession relationships
   - Show data flow between components

3. **User Guide: Session Reuse Patterns**
   ```markdown
   ## When to Use Persistent Browser Sessions
   
   ### Good Use Cases:
   - Multi-step workflows that need to maintain login state
   - Debugging sessions requiring live browser access
   - Workflows that benefit from preserved cookies/localStorage
   
   ### Bad Use Cases:
   - Isolated workflows with no shared state requirements
   - High-security workflows (use fresh browsers instead)
   - Workflows with conflicting browser configurations
   ```

4. **Troubleshooting Guide**
   ```markdown
   ## Common Issues
   
   ### "Browser session not found"
   - Verify the session ID is correct
   - Check that the session hasn't expired
   - Ensure you're using the same organization ID
   
   ### "Session occupied by another run"
   - Wait for the current workflow to complete
   - Manually release the session via API
   - Use a different browser session ID
   ```

## Conclusion and Next Steps

### Summary of Findings
1. **Integration is Well-Designed**: Clear separation of concerns between PBS, WorkflowRun, and DebugSession
2. **Lifecycle Management**: Proper begin/release pattern prevents resource leaks
3. **VNC Streaming**: Seamless integration with debugging workflows
4. **Validation**: Good error handling for missing/expired sessions

### Recommended Next Steps

1. **Add Comprehensive Unit Tests**
   - Test workflow run creation with existing PBS
   - Test debug session lifecycle and PBS creation
   - Test cleanup and session release
   - Test error cases (expired sessions, concurrent access)

2. **Improve Error Handling**
   - Add explicit locking for concurrent access prevention
   - Auto-recreate PBS when debug session's PBS expires
   - Add heartbeat mechanism for stale occupation detection

3. **Enhance Documentation**
   - Update API documentation with examples
   - Create architecture diagrams
   - Write user guides and troubleshooting documentation

4. **Performance Optimizations**
   - Implement lazy VNC streaming (only when needed)
   - Add session pooling for faster startup
   - Monitor memory usage and add auto-scaling

5. **Security Improvements**
   - Add VNC authentication
   - Implement session ID rotation
   - Add browser state sanitization after workflow completion

### Files to Review for Implementation

1. **Core Integration**: `skyvern/forge/sdk/workflow/service.py` (lines 2371-2419, 746-850, 3024-3061)
2. **PBS Management**: `skyvern/webeye/default_persistent_sessions_manager.py` (lines 295-337)
3. **Browser Manager**: `skyvern/webeye/real_browser_manager.py` (lines 356-400)
4. **Debug Sessions**: `skyvern/services/debug_session_service.py`, `skyvern/forge/sdk/routes/debug_sessions.py`
5. **Database**: `skyvern/forge/sdk/db/agent_db.py` (lines 2639, 5370-5515)

This analysis provides a complete understanding of the PBS integration with WorkflowRunV2 and StreamingService in Skyvern. The architecture is solid but could benefit from additional tests, documentation, and some edge case handling improvements.
