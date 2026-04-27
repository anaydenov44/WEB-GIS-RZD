import json
import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE_PATH = LOGS_DIR / "route_matcher.log"
LOGGER_NAME = "railway_gis.matcher"


def get_matcher_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    return logger


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def build_exception_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }


def append_error(
    diagnostics: dict[str, Any] | None,
    *,
    stage: str,
    exc: BaseException,
    extra: dict[str, Any] | None = None,
) -> None:
    if diagnostics is None:
        return

    errors = diagnostics.setdefault("errors", [])
    payload = {
        "stage": stage,
        **build_exception_payload(exc),
    }

    if extra:
        payload["extra"] = extra

    errors.append(payload)


def log_event(
    level: str,
    event: str,
    **fields: Any,
) -> None:
    logger = get_matcher_logger()

    payload = {
        "event": event,
        **fields,
    }

    line = safe_json_dumps(payload)

    if level == "debug":
        logger.debug(line)
    elif level == "warning":
        logger.warning(line)
    elif level == "error":
        logger.error(line)
    else:
        logger.info(line)


class StageTimer:
    def __init__(
        self,
        stage_name: str,
        *,
        diagnostics: dict[str, Any] | None = None,
        logger_context: dict[str, Any] | None = None,
    ) -> None:
        self.stage_name = stage_name
        self.diagnostics = diagnostics
        self.logger_context = logger_context or {}
        self.started_at = 0.0

    def __enter__(self):
        self.started_at = perf_counter()
        log_event(
            "info",
            "stage_start",
            stage=self.stage_name,
            **self.logger_context,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = round((perf_counter() - self.started_at) * 1000, 2)

        if self.diagnostics is not None:
            timings = self.diagnostics.setdefault("timings_ms", {})
            timings[self.stage_name] = duration_ms

        if exc is None:
            log_event(
                "info",
                "stage_finish",
                stage=self.stage_name,
                duration_ms=duration_ms,
                **self.logger_context,
            )
            return False

        append_error(
            self.diagnostics,
            stage=self.stage_name,
            exc=exc,
        )

        log_event(
            "error",
            "stage_error",
            stage=self.stage_name,
            duration_ms=duration_ms,
            exception=build_exception_payload(exc),
            **self.logger_context,
        )
        return False