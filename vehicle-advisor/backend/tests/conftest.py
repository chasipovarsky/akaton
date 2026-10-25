import time

import pytest


def wait_turn(client, sid: str, timeout: float = 10.0) -> dict:
    """POST /api/chat חוזר מיד; מחכים (polling כמו הדפדפן) עד שהתור ברקע מסתיים."""
    end = time.monotonic() + timeout
    while True:
        st = client.get(f"/api/chat/{sid}/state").json()
        if not st["busy"]:
            return st
        if time.monotonic() > end:
            raise AssertionError(f"turn still busy after {timeout}s")
        time.sleep(0.02)


@pytest.fixture
def wait():
    return wait_turn
