import time
import threading
import logging
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

class QuotaTracker:
    """
    Thread-safe shared quota tracker for all Groq API calls.
    Enforces rate limits at the system level across all agents.

    Groq free tier:
    - 30 RPM for 8B models (llama-3.1-8b-instant)
    - 10 RPM for 70B models (llama-3.3-70b-versatile)
    - 14,400 RPD total
    """

    GROQ_RPM = 30           # 8B models
    GROQ_RPM_LARGE = 10     # 70B models
    GROQ_RPD = 14400
    SAFETY_MARGIN = 0.85

    def __init__(self):
        self._lock = threading.Lock()
        self._minute_requests = []
        self._day_requests = 0
        self._day_reset = date.today()
        self._total_calls = 0
        self._total_waits = 0
        self._total_wait_time = 0.0

    def acquire(self, agent_name: str = "unknown", large_model: bool = False) -> bool:
        """
        Block until a Groq request slot is available.
        large_model=True uses the stricter 70B RPM limit.
        Returns True when safe to proceed.
        """
        rpm_limit_raw = self.GROQ_RPM_LARGE if large_model else self.GROQ_RPM

        while True:
            with self._lock:
                now = time.time()
                today = date.today()

                # Reset daily counter if new day
                if today > self._day_reset:
                    logger.info(f"QuotaTracker: New day — resetting daily counter ({self._day_requests} calls yesterday)")
                    self._day_requests = 0
                    self._day_reset = today

                # Check daily limit
                daily_limit = int(self.GROQ_RPD * self.SAFETY_MARGIN)
                if self._day_requests >= daily_limit:
                    next_midnight = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=1)
                    wait_seconds = int((next_midnight - datetime.now()).total_seconds())
                    logger.warning(f"QuotaTracker: Daily limit reached ({self._day_requests}/{daily_limit}). Resets in {wait_seconds//3600}h {(wait_seconds%3600)//60}m")
                    raise QuotaExhaustedError("Daily Groq quota exhausted. Resets at midnight.")

                # Clean up minute window
                minute_ago = now - 60.0
                self._minute_requests = [t for t in self._minute_requests if t > minute_ago]

                # Check per-minute limit
                rpm_limit = int(rpm_limit_raw * self.SAFETY_MARGIN)
                if len(self._minute_requests) < rpm_limit:
                    self._minute_requests.append(now)
                    self._day_requests += 1
                    self._total_calls += 1
                    return True

                # No slot — calculate wait
                oldest = min(self._minute_requests)
                wait = 60.0 - (now - oldest) + 0.5

            logger.info(f"QuotaTracker [{agent_name}]: RPM limit ({rpm_limit}) reached. Waiting {wait:.1f}s")
            self._total_waits += 1
            self._total_wait_time += wait
            time.sleep(wait)

    def get_stats(self) -> dict:
        with self._lock:
            now = time.time()
            minute_ago = now - 60.0
            recent = [t for t in self._minute_requests if t > minute_ago]
            daily_limit = int(self.GROQ_RPD * self.SAFETY_MARGIN)
            return {
                "requests_this_minute": len(recent),
                "requests_today": self._day_requests,
                "daily_limit": daily_limit,
                "daily_remaining": daily_limit - self._day_requests,
                "total_calls": self._total_calls,
                "total_waits": self._total_waits,
                "total_wait_time_seconds": round(self._total_wait_time, 1)
            }

    def log_stats(self):
        stats = self.get_stats()
        logger.info(
            f"QuotaTracker: {stats['requests_this_minute']} RPM | "
            f"{stats['requests_today']}/{stats['daily_limit']} today | "
            f"{stats['daily_remaining']} remaining | "
            f"{stats['total_waits']} waits ({stats['total_wait_time_seconds']}s total)"
        )


class QuotaExhaustedError(Exception):
    pass


# Global singleton — shared across all agents
groq_quota = QuotaTracker()