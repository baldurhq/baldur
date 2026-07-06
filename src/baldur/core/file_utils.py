import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def safe_unlink(path: Path) -> bool:
    """Multi-worker safe file deletion.

    Returns:
        True if actually deleted, False if already deleted or permission error.
    """
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        logger.warning("safe_unlink.permission_denied", extra={"path": str(path)})
        return False
    except OSError as e:
        logger.warning(
            "safe_unlink.os_failed", extra={"path": str(path), "error": str(e)}
        )
        return False
