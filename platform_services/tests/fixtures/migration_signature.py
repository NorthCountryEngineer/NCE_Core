"""Produced by Proposal Studio sign_document using deterministic test-only bytes(range(32)).

Only the public key and signed message are retained; this is interoperability
evidence, not a live identity or a source of runtime authority.
"""
FIXTURE = {
    'public_key': 'A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg',  # pragma: allowlist secret -- public deterministic Ed25519 test key
    'envelope': {
        'protocol': 'nce-app-migration/v1',
        'purpose': 'migration-receipt',
        'signer_key_id': 'sha256:56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c',
        'payload': {'note': 'interop', 'count': 1},
        'signature': 'JXJC3jV4hIS8DZH2Cm0P06E9BpZNCne30uDYITqORGaE04gw_LI1dUmAZDA-yXA8qBRpJqTWkT0BATKfN0eyCA',  # pragma: allowlist secret -- public signature over deterministic test metadata
    },
}
