"""Shared Meross BLE GATT slot with Identify-over-history priority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .const import GATT_YIELD_SLOT_COOLDOWN


class MerossBleGattGate:
    """One adapter slot for all Meross BLE GATT work.

    Identify/bind marks itself waiting before acquiring the lock so an in-flight
    MS120 history transfer can finish the current page, release the slot, and
    reschedule. After history disconnects, wait briefly so BlueZ can free the
    adapter before Identify connects.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._identify_waiters = 0
        self._slot_cooldown_until = 0.0

    @property
    def identify_waiting(self) -> bool:
        """True while at least one Identify/bind wants the GATT slot."""
        return self._identify_waiters > 0

    def note_disconnected(self) -> None:
        """Start adapter cooldown after any GATT disconnect."""
        self._slot_cooldown_until = (
            asyncio.get_running_loop().time() + GATT_YIELD_SLOT_COOLDOWN
        )

    async def _async_wait_slot_cooldown(self) -> None:
        remaining = self._slot_cooldown_until - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def claim_identify(self) -> None:
        """Mark Identify as waiting so history stops at the next page boundary."""
        self._identify_waiters += 1

    def release_identify(self) -> None:
        """Drop an Identify claim (must pair with claim_identify)."""
        if self._identify_waiters > 0:
            self._identify_waiters -= 1

    @asynccontextmanager
    async def identify_claim(self) -> AsyncIterator[None]:
        """Hold Identify priority from advertisement wait through GATT."""
        self.claim_identify()
        try:
            yield
        finally:
            self.release_identify()

    @asynccontextmanager
    async def identify_session(self) -> AsyncIterator[None]:
        """Acquire the GATT lock. Caller must already hold an Identify claim."""
        async with self._lock:
            await self._async_wait_slot_cooldown()
            yield

    @asynccontextmanager
    async def history_session(self) -> AsyncIterator[None]:
        """Low-priority GATT session; waits for Identify and yields if it arrives."""
        while True:
            while self._identify_waiters > 0:
                await asyncio.sleep(0.25)
            await self._lock.acquire()
            if self._identify_waiters > 0:
                self._lock.release()
                continue
            try:
                await self._async_wait_slot_cooldown()
                yield
            finally:
                self._lock.release()
            return
