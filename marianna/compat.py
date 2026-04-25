from astrbot.api import logger

try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    aiofiles = None
    AIOFILES_AVAILABLE = False
    logger.warning("aiofiles is not installed; falling back to synchronous file IO")
