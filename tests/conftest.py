import pytest

from transfer import forwarder


@pytest.fixture(autouse=True)
def _reset_forwarder_flood_state():
    """每个用例前关闭转发限速并清空限流冷却，保证用例相互隔离、不因限速变慢。"""
    forwarder.FORWARD_INTERVAL = 0
    forwarder.reset_flood_state()
    yield
    forwarder.reset_flood_state()
