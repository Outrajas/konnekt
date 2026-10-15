import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from leadforge.db import connect
from leadforge.web.errors import BudgetExhausted, DomainPaused
from leadforge.web.policy import DomainRule


class Budget:
    _locks: dict[tuple[int, str], asyncio.Lock] = {}

    def __init__(
        self,
        db_path: str,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.db_path = db_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.uniform = uniform

    async def acquire(self, host: str, rule: DomainRule) -> None:
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault((id(loop), host), asyncio.Lock())
        async with lock:
            now = self.clock().astimezone(UTC)
            conn = connect(self.db_path)
            try:
                row = conn.execute("SELECT * FROM domain_state WHERE host=?", (host,)).fetchone()
                if row and row["paused_until"]:
                    until = datetime.fromisoformat(row["paused_until"])
                    if until > now:
                        raise DomainPaused(
                            f"{host} paused until {until.isoformat()}: {row['pause_reason']}"
                        )
                day = now.date().isoformat()
                count = int(row["count_today"]) if row and row["day"] == day else 0
                if count >= rule.daily_cap:
                    raise BudgetExhausted(
                        f"Daily request cap reached for {host} ({rule.daily_cap})"
                    )
                if row and row["last_request_at"] and row["day"] == day:
                    last = datetime.fromisoformat(row["last_request_at"])
                    wait_seconds = (last + timedelta(seconds=self.uniform(
                        rule.min_delay_s, rule.max_delay_s
                    )) - now).total_seconds()
                    if wait_seconds > 0:
                        await self.sleep(wait_seconds)
                        now = self.clock().astimezone(UTC)
                conn.execute(
                    """INSERT INTO domain_state(host, day, count_today, last_request_at)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(host) DO UPDATE SET
                         count_today=CASE WHEN domain_state.day=excluded.day
                           THEN domain_state.count_today + 1 ELSE 1 END,
                         day=excluded.day, last_request_at=excluded.last_request_at""",
                    (host, now.date().isoformat(), now.isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

    async def pause(self, host: str, hours: int, reason: str) -> None:
        now = self.clock().astimezone(UTC)
        until = (now + timedelta(hours=hours)).isoformat()
        conn = connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO domain_state(host, paused_until, pause_reason)
                   VALUES (?, ?, ?) ON CONFLICT(host) DO UPDATE SET
                   paused_until=excluded.paused_until, pause_reason=excluded.pause_reason""",
                (host, until, reason),
            )
            conn.commit()
        finally:
            conn.close()


def daily_usage(db_path: str, host: str, day: str | None = None) -> int:
    day = day or datetime.now(UTC).date().isoformat()
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT count_today FROM domain_state WHERE host=? AND day=?", (host, day)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
