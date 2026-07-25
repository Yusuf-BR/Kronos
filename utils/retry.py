import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)


class DailyQuotaExceeded(Exception):
    """
    Raised when an error indicates a DAILY token/request quota is exhausted
    (not a per-minute/per-second rate limit). Retrying within the same call
    cannot fix this — the caller should fall back to a different
    provider/model instead of burning retry attempts and time.
    """
    pass


def _classify_error(error_str: str) -> str:
    """
    Returns one of: "daily_quota", "rate_limit", "network", "unknown"
    """
    if (
        "tokens per day" in error_str
        or "requests per day" in error_str
        or "tpd" in error_str
        or "rpd" in error_str
        or ("per day" in error_str and ("rate_limit" in error_str or "quota" in error_str))
    ):
        return "daily_quota"

    if "429" in error_str or "rate limit" in error_str or "rate_limit" in error_str or "resource_exhausted" in error_str:
        return "rate_limit"

    if "connection" in error_str or "timeout" in error_str or "network" in error_str:
        return "network"

    return "unknown"


def with_retry(max_attempts: int = 5, base_delay: float = 2.0,
               exceptions: tuple = (Exception,)):
    """
    Decorator for exponential backoff retry logic.
    Handles rate limits and transient failures.

    Fails FAST (raises DailyQuotaExceeded, no retries) if the error looks
    like a daily quota being exhausted — backoff cannot fix that within
    the same run.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            last_exception = None

            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    attempt += 1
                    last_exception = e
                    error_str = str(e).lower()
                    kind = _classify_error(error_str)

                    if kind == "daily_quota":
                        logger.warning(f"Daily quota exhausted on {func.__name__}, not retrying: {e}")
                        raise DailyQuotaExceeded(str(e)) from e

                    if attempt >= max_attempts:
                        break

                    if kind == "rate_limit":
                        wait = base_delay * (2 ** (attempt - 1))
                        logger.warning(f"Rate limit hit on {func.__name__}, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                    elif kind == "network":
                        wait = base_delay * attempt
                        logger.warning(f"Network error on {func.__name__}, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                    else:
                        wait = base_delay
                        logger.warning(f"Error on {func.__name__} attempt {attempt}/{max_attempts}: {e}. Waiting {wait:.1f}s")

                    time.sleep(wait)

            logger.error(f"{func.__name__} failed after {max_attempts} attempts. Last error: {last_exception}")
            raise last_exception

        return wrapper
    return decorator


def call_with_retry(func, *args, max_attempts: int = 5, base_delay: float = 2.0, **kwargs):
    """
    Non-decorator version for inline use.

    Fails FAST (raises DailyQuotaExceeded, no retries) if the error looks
    like a daily quota being exhausted — callers can catch this specifically
    to fall back to a different provider instead of retrying pointlessly.
    """
    attempt = 0
    last_exception = None

    while attempt < max_attempts:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            attempt += 1
            last_exception = e
            error_str = str(e).lower()
            kind = _classify_error(error_str)

            if kind == "daily_quota":
                logger.warning(f"Daily quota exhausted, not retrying: {e}")
                raise DailyQuotaExceeded(str(e)) from e

            if attempt >= max_attempts:
                break

            if kind == "rate_limit":
                wait = base_delay * (2 ** (attempt - 1))
                logger.warning(f"Rate limit hit, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
            elif kind == "network":
                wait = base_delay * attempt
                logger.warning(f"Network error, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
            else:
                wait = base_delay
                logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}. Waiting {wait:.1f}s")

            time.sleep(wait)

    logger.error(f"All {max_attempts} attempts failed. Last error: {last_exception}")
    raise last_exception