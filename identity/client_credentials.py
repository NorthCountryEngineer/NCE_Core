"""Reuse machine-secret verification without caching authorization state."""

import hmac
import secrets
import threading
import time
from collections import OrderedDict

from django.contrib.auth.hashers import check_password


class ResourceClientSecretVerifier:
    """Bounded, process-local proofs bound to the freshly read credential hash.

    Callers must load and validate the current application on every request.
    Entries contain only a keyed digest and expiry: no secrets, password hashes,
    tokens, application objects, or authorization decisions are retained.
    """

    def __init__(self, *, max_entries=256, ttl=600, clock=time.monotonic):
        self._key = secrets.token_bytes(32)
        self._max_entries = max_entries
        self._ttl = ttl
        self._clock = clock
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        # Fixed stripes bound lock memory even for arbitrary invalid credentials.
        self._verification_locks = tuple(threading.Lock() for _ in range(32))

    def _fingerprint(self, client_id, supplied_secret, stored_hash):
        digest = hmac.new(self._key, digestmod="sha256")
        for value in (str(client_id), supplied_secret, stored_hash):
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.digest()

    def _cached(self, fingerprint):
        with self._lock:
            expiry = self._entries.get(fingerprint)
            if expiry is None:
                return False
            if expiry <= self._clock():
                del self._entries[fingerprint]
                return False
            self._entries.move_to_end(fingerprint)
            return True

    def verify(self, client_id, supplied_secret, stored_hash):
        fingerprint = self._fingerprint(client_id, supplied_secret, stored_hash)
        if self._cached(fingerprint):
            return True
        # Collapse concurrent cache misses for the same credential, keeping the
        # dictionary lock free while Django performs its normal expensive hash.
        with self._verification_locks[fingerprint[0] % len(self._verification_locks)]:
            if self._cached(fingerprint):
                return True
            if not check_password(supplied_secret, stored_hash):
                return False
            with self._lock:
                self._entries[fingerprint] = self._clock() + self._ttl
                self._entries.move_to_end(fingerprint)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            return True


resource_client_secrets = ResourceClientSecretVerifier()
