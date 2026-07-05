import time
import threading
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

class QuotaTracker:
    """
    Thread-safe shared quota tracker for all Groq API calls.
    Enforces rate limits at the system level across all agents.
    
    Groq free tier:
    - 30 requests per minute (RPM)
    - 14,400 requests per day (RPD)
    - 30,000 tokens per minute (TPM) — not tracked here, RPM is the bottleneck
    """

    GROQ_RPM = 30           # requests per minute
    GROQ_RPD = 14400        # requests per day
    SAFETY_MARGIN = 0.85    # use only 85% of limit to avoid edge cases

    def __init__(self):
        self._lock = threading.Lock()
        self._minute_requests = []   # timestamps of requests in current minute
        self._day_requests = 0       # total requests today
        self._day_reset = date.today()
        self._total_calls = 0
        self._total_waits = 0
        self._total_wait_time = 0.0

    def acquire(self, agent_name: str = "unknown") -> bool:
        """
        Block until a Groq request slot is available.
        Returns True when safe to proceed.
        """
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
                    # Calculate seconds until midnight
                    midnight = datetime.combine(
                        datetime.now().date(), 
                        datetime.min.time()
                    ).replace(hour=0, minute=0, second=0)
                    from datetime import timedelta
                    next_midnight = midnight + timedelta(days=1)
                    wait_seconds = (next_midnight - datetime.now()).seconds
                    logger.warning(f"QuotaTracker: Daily limit reached ({self._day_requests}/{daily_limit}). Resets in {wait_seconds//3600}h {(wait_seconds%3600)//60}m")
                    # Don't block forever — raise to let caller handle
                    raise QuotaExhaustedError(f"Daily Groq quota exhausted. Resets at midnight.")

                # Clean up minute window — keep only last 60 seconds
                minute_ago = now - 60.0
                self._minute_requests = [t for t in self._minute_requests if t > minute_ago]

                # Check per-minute limit
                rpm_limit = int(self.GROQ_RPM * self.SAFETY_MARGIN)
                if len(self._minute_requests) < rpm_limit:
                    # Slot available — consume it
                    self._minute_requests.append(now)
                    self._day_requests += 1
                    self._total_calls += 1
                    return True

                # No slot available — calculate wait time
                oldest = min(self._minute_requests)
                wait = 60.0 - (now - oldest) + 0.5  # +0.5s buffer

            # Wait outside the lock
            logger.info(f"QuotaTracker [{agent_name}]: RPM limit reached ({len(self._minute_requests)}/{rpm_limit}). Waiting {wait:.1f}s")
            self._total_waits += 1
            self._total_wait_time += wait
            time.sleep(wait)

    def get_stats(self) -> dict:
        with self._lock:
            now = time.time()
            minute_ago = now - 60.0
            recent = [t for t in self._minute_requests if t > minute_ago]
            daily_limit = int(self.GROQ_RPD * self.SAFETY_MARGIN)
            rpm_limit = int(self.GROQ_RPM * self.SAFETY_MARGIN)
            return {
                "requests_this_minute": len(recent),
                "rpm_limit": rpm_limit,
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
            f"QuotaTracker: {stats['requests_this_minute']}/{stats['rpm_limit']} RPM | "
            f"{stats['requests_today']}/{stats['daily_limit']} today | "
            f"{stats['daily_remaining']} remaining | "
            f"{stats['total_waits']} waits ({stats['total_wait_time_seconds']}s total)"
        )


class QuotaExhaustedError(Exception):
    pass


# Global singleton — shared across all agents
groq_quota = QuotaTracker()