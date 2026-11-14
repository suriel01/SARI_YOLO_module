"""REST transport to the brain: POST /api/alerts/event authenticated with X-SARI-Node-Key."""
from __future__ import annotations

import logging

import requests

from ..config import Settings

logger = logging.getLogger(__name__)

NODE_KEY_HEADER = "X-SARI-Node-Key"


def post_alert(settings: Settings, payload: dict, timeout: float = 3.0) -> bool:
    """Deliver one alert to the single configured brain URL. True only on a 2xx answer."""
    try:
        response = requests.post(
            settings.cerebro_http_event_url,
            json=payload,
            headers={NODE_KEY_HEADER: settings.node_key},
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("Brain REST alert failed: %s", type(exc).__name__)
        return False
    if 200 <= response.status_code < 300:
        logger.info("Brain REST alert accepted (HTTP %s)", response.status_code)
        return True
    if response.status_code in (401, 403):
        logger.error("Brain rejected the node key (HTTP %s): check SARI_NODE_KEY", response.status_code)
    else:
        logger.warning("Brain REST alert rejected (HTTP %s)", response.status_code)
    return False
