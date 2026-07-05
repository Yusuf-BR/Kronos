import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

def with_retry(max_attempts: int = 5, base_delay: float = 2.0, 
               exceptions: tuple = (Exception,)):
    """
    Decorator for exponential backoff retry logic.
    Handles rate limits and transient failures.
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

                    # Rate limit — use retry-after if available
                    if "429" in str(e) or "rate limit" in error_str or "resource_exhausted" in error_str:
                        wait = base_delay * (2 ** attempt)
                        logger.warning(f"Rate limit hit on {func.__name__}, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                        time.sleep(wait)

                    # Transient network error
                    elif "connection" in error_str or "timeout" in error_str or "network" in error_str:
                        wait = base_delay * attempt
                        logger.warning(f"Network error on {func.__name__}, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                        time.sleep(wait)

                    # Unknown error — short wait
                    else:
                        if attempt < max_attempts:
                            wait = base_delay
                            logger.warning(f"Error on {func.__name__} attempt {attempt}/{max_attempts}: {e}. Waiting {wait:.1f}s")
                            time.sleep(wait)

            logger.error(f"{func.__name__} failed after {max_attempts} attempts. Last error: {last_exception}")
            raise last_exception

        return wrapper
    return decorator


def call_with_retry(func, *args, max_attempts=5, base_delay=2.0, **kwargs):
    """
    Non-decorator version for inline use.
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

            if "429" in str(e) or "rate limit" in error_str or "resource_exhausted" in error_str:
                wait = base_delay * (2 ** attempt)
                logger.warning(f"Rate limit hit, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                time.sleep(wait)
            elif "connection" in error_str or "timeout" in error_str:
                wait = base_delay * attempt
                logger.warning(f"Network error, attempt {attempt}/{max_attempts}. Waiting {wait:.1f}s")
                time.sleep(wait)
            else:
                if attempt < max_attempts:
                    logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}")
                    time.sleep(base_delay)

    logger.error(f"All {max_attempts} attempts failed. Last error: {last_exception}")
    raise last_exception