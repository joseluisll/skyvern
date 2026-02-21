"""
Test to verify that get_browser_address() returns a default address in local mode
without waiting indefinitely.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager


async def test_get_browser_address_local_mode() -> None:
    """Test that get_browser_address returns default address in local mode."""
    # Create a mock database session
    mock_db = MagicMock()

    # Create the manager
    manager = DefaultPersistentSessionsManager(database=mock_db)

    # Mock settings to be in local mode
    with patch("skyvern.webeye.default_persistent_sessions_manager.settings") as mock_settings:
        mock_settings.ENV = "local"

        # Call get_browser_address - it should return immediately with default address
        result = await manager.get_browser_address(session_id="test_session_id", organization_id="test_org_id")

        # Verify it returns the expected default address
        assert result == "0.0.0.0:9223", f"Expected '0.0.0.0:9223', got '{result}'"
        print("✓ Test passed: get_browser_address returns default address in local mode")


async def test_get_browser_address_cloud_mode() -> None:
    """Test that get_browser_address calls wait_on_persistent_browser_address in cloud mode."""
    # Create a mock database session
    mock_db = MagicMock()

    # Create the manager
    manager = DefaultPersistentSessionsManager(database=mock_db)

    # Mock settings to be in production mode
    with patch("skyvern.webeye.default_persistent_sessions_manager.settings") as mock_settings:
        mock_settings.ENV = "production"

        # Mock the wait_on_persistent_browser_address function
        mock_wait = AsyncMock(return_value="192.168.1.100:9223")
        with patch("skyvern.webeye.default_persistent_sessions_manager.wait_on_persistent_browser_address", mock_wait):
            result = await manager.get_browser_address(session_id="test_session_id", organization_id="test_org_id")

            # Verify it called the wait function
            assert mock_wait.called, "wait_on_persistent_browser_address should be called in cloud mode"
            assert result == "192.168.1.100:9223", f"Expected '192.168.1.100:9223', got '{result}'"
            print("✓ Test passed: get_browser_address calls wait function in cloud mode")


async def test_get_browser_address_cloud_mode_none() -> None:
    """Test that get_browser_address raises MissingBrowserAddressError when address is None in cloud mode."""
    # Create a mock database session
    mock_db = MagicMock()

    # Create the manager
    manager = DefaultPersistentSessionsManager(database=mock_db)

    # Mock settings to be in production mode
    with patch("skyvern.webeye.default_persistent_sessions_manager.settings") as mock_settings:
        mock_settings.ENV = "production"

        # Mock the wait_on_persistent_browser_address function to return None
        mock_wait = AsyncMock(return_value=None)
        with patch("skyvern.webeye.default_persistent_sessions_manager.wait_on_persistent_browser_address", mock_wait):
            try:
                await manager.get_browser_address(session_id="test_session_id", organization_id="test_org_id")
                assert False, "Should have raised MissingBrowserAddressError"
            except Exception as e:
                # Verify it raised the expected error
                assert "MissingBrowserAddressError" in str(type(e).__name__), (
                    f"Expected MissingBrowserAddressError, got {type(e)}"
                )
                print("✓ Test passed: get_browser_address raises error when address is None in cloud mode")


async def main() -> None:
    """Run all tests."""
    print("Running browser address fix tests...\n")

    await test_get_browser_address_local_mode()
    await test_get_browser_address_cloud_mode()
    await test_get_browser_address_cloud_mode_none()

    print("\n✓ All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
