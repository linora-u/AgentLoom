"""Runtime-neutral logging levels and parsing."""

from __future__ import annotations

import logging
from enum import IntEnum


class AgentLoomLogLevel(IntEnum):
    """Standard log levels used by AgentLoom logging policy."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    OFF = 50

    @classmethod
    def from_str(cls, value: str) -> AgentLoomLogLevel:
        normalized = value.strip().upper()
        mapping = {
            "DEBUG": cls.DEBUG,
            "INFO": cls.INFO,
            "WARNING": cls.WARNING,
            "WARN": cls.WARNING,
            "ERROR": cls.ERROR,
            "CRITICAL": cls.ERROR,
            "OFF": cls.OFF,
            "DISABLE": cls.OFF,
            "DISABLED": cls.OFF,
        }
        if normalized not in mapping:
            raise ValueError(f"Unknown log level: {value!r}")
        return mapping[normalized]

    @classmethod
    def from_int(cls, value: int) -> AgentLoomLogLevel:
        if value >= logging.CRITICAL:
            return cls.OFF
        if value >= logging.ERROR:
            return cls.ERROR
        if value >= logging.WARNING:
            return cls.WARNING
        if value >= logging.INFO:
            return cls.INFO
        return cls.DEBUG
