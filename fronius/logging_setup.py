"""Logging configuration for Fronius Modbus MQTT"""

import logging
from logging.handlers import RotatingFileHandler
import sys
from typing import Optional

_logger: Optional[logging.Logger] = None


def setup_logging(log_level: str = "INFO", log_file: str = None) -> logging.Logger:
    """
    Setup and configure logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for logging output

    Returns:
        Configured logger instance
    """
    global _logger

    # Create logger
    logger = logging.getLogger("fronius_modbus_mqtt")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear any existing handlers
    logger.handlers = []

    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional) with rotation: 5MB per file, 3 backups (20MB max)
    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    """
    Get the configured logger instance.

    Returns:
        Logger instance, creates default if not configured
    """
    global _logger

    if _logger is None:
        _logger = setup_logging()

    return _logger
