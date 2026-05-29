"""Thin async Kafka wrapper + audit emitter.

Wraps aiokafka so services aren't directly coupled to the client library.
If we swap Kafka for Redpanda or a managed service later, only this file changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Callable, Type, TypeVar

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import BaseModel, ValidationError

from veilwatch_contracts.events import AuditAction, AuditRecord
from veilwatch_contracts.topics import Topics

log = logging.getLogger(__name__)
E = TypeVar("E", bound=BaseModel)


def _bootstrap() -> str:
    return os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


class EventBus:
    """Single producer per service; one consumer per subscribed topic."""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self._producer: AIOKafkaProducer | None = None
        self._consumers: list[AIOKafkaConsumer] = []

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=_bootstrap(),
            client_id=f"{self.service_name}-producer",
            enable_idempotence=True,  # no duplicate audit records on producer retry
            acks="all",
            linger_ms=5,
        )
        await self._producer.start()
        log.info("kafka producer started", extra={"service": self.service_name})

    async def stop(self) -> None:
        for c in self._consumers:
            await c.stop()
        if self._producer is not None:
            await self._producer.stop()

    async def publish(self, topic: str, event: BaseModel, *, key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("EventBus.start() must be called first")
        payload = event.model_dump_json().encode("utf-8")
        await self._producer.send_and_wait(
            topic,
            payload,
            key=key.encode("utf-8") if key else None,
        )

    async def audit(
        self,
        *,
        action: AuditAction,
        subject_id: str,
        correlation_ids: dict[str, str] | None = None,
        actor_id: str = "system",
        payload: dict | None = None,
    ) -> None:
        """Append an immutable audit record. Constraint #3: MUST be awaited
        before any alert or dispatch side-effect. Do not call in fire-and-forget."""
        import uuid
        record = AuditRecord(
            event_id=str(uuid.uuid4()),
            service=self.service_name,
            action=action,
            subject_id=subject_id,
            correlation_ids=correlation_ids or {},
            actor_id=actor_id,
            payload=payload or {},
        )
        await self.publish(Topics.AUDIT_LOG, record, key=subject_id)

    async def consume(
        self,
        topic: str,
        group_id: str,
        schema: Type[E],
        dlq_topic: str | None = None,
    ) -> AsyncIterator[E]:
        """Async iterator over parsed events. Parse failures → DLQ (if provided)
        and the offset is still committed so the group doesn't loop forever on
        a poison pill."""
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=_bootstrap(),
            client_id=f"{self.service_name}-{group_id}",
            group_id=group_id,
            enable_auto_commit=False,
            auto_offset_reset="latest",
        )
        self._consumers.append(consumer)
        await consumer.start()
        log.info("kafka consumer started",
                 extra={"service": self.service_name, "topic": topic, "group_id": group_id})
        try:
            async for msg in consumer:
                try:
                    event = schema.model_validate_json(msg.value)
                except ValidationError as e:
                    log.error("schema validation failed; routing to DLQ",
                              extra={"topic": topic, "error": str(e)})
                    if dlq_topic and self._producer is not None:
                        try:
                            await self._producer.send_and_wait(
                                dlq_topic,
                                json.dumps({
                                    "raw": msg.value.decode("utf-8", errors="replace"),
                                    "error": str(e),
                                    "source_topic": topic,
                                }).encode("utf-8"),
                                key=msg.key,
                            )
                        except Exception:
                            log.exception("failed to write to DLQ; dropping")
                    await consumer.commit()
                    continue
                try:
                    yield event
                except Exception:
                    log.exception("handler raised; message will be retried after restart")
                    raise
                else:
                    await consumer.commit()
        finally:
            await consumer.stop()
