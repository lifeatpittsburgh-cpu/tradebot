from .base import DataProvider, drop_incomplete_bar, normalize_ohlcv
from .cache import CachedProvider
from .yfinance_provider import YFinanceProvider


def default_provider(cache_dir=None, ttl_minutes: float = 60) -> DataProvider:
    """yfinance wrapped in an on-disk cache (refreshed at most every `ttl_minutes`)."""
    provider: DataProvider = YFinanceProvider()
    if cache_dir:
        provider = CachedProvider(provider, cache_dir, ttl_minutes)
    return provider


__all__ = ["DataProvider", "YFinanceProvider", "CachedProvider", "normalize_ohlcv", "drop_incomplete_bar", "default_provider"]
