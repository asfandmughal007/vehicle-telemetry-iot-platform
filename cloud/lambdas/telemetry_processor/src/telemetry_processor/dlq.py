"""Dead-letter queue for messages that can never be processed."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

#: SQS allows 256 KiB per message; keep the original well below that.
_MAX_ORIGINAL_CHARS = 100_000


class DeadLetterQueue:
    """Sends rejected messages to SQS together with the reason, for later inspection."""

    def __init__(self, sqs_client: Any, queue_url: str) -> None:
        self._sqs = sqs_client
        self._queue_url = queue_url

    def send(self, reason: str, details: list[str], original: object) -> bool:
        """Best effort: a failing DLQ must not turn a handled bad message into a crash.

        Returns ``True`` if the message was queued. On failure the complete context is in
        the error log line, so the message is still recoverable from CloudWatch.
        """
        body = json.dumps(
            {
                "reason": reason,
                "details": details,
                "rejected_at": datetime.now(UTC).isoformat(),
                "original": json.dumps(original, default=str)[:_MAX_ORIGINAL_CHARS],
            }
        )
        try:
            self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=body)
        except (ClientError, BotoCoreError):
            logger.exception("dlq_send_failed", extra={"reason": reason, "details": details})
            return False
        return True
