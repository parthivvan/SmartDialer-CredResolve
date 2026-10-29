"""Provider contract. The dialer only ever talks to this interface, never to provider internals.

Everything is keyed by OUR call_ref (= calls.id). That gives:
  * idempotent place_call (retry/redrive after a crash or a timeout never double-dials)
  * events that can be matched to a call even before we've stored the provider's own call id
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class ProviderError(Exception):
    """Definite failure: the call was NOT placed."""


class ProviderTimeout(ProviderError):
    """Ambiguous failure: the call MAY have been placed. Callers must issue a hangup(call_ref) to be safe."""


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    call_ref: str
    event_type: str            # RINGING | ANSWERED | COMPLETED | NO_ANSWER | BUSY | FAILED
    provider_ts: float
    reason: Optional[str] = None
    provider_call_id: Optional[str] = None


class TelecomProvider(ABC):
    name: str

    @abstractmethod
    def place_call(self, call_ref: str, to: str) -> str: ...

    @abstractmethod
    def hangup(self, call_ref: str) -> None: ...

    @abstractmethod
    def get_status(self, call_ref: str) -> str:
        """UNKNOWN | INITIATED | RINGING | ANSWERED | COMPLETED | NO_ANSWER | BUSY. May raise ProviderError."""

    @abstractmethod
    def poll_events(self) -> list:
        """Stand-in for webhooks: returns events that have 'arrived'. In production a webhook endpoint calls
        EventHandler.handle(event) directly."""
