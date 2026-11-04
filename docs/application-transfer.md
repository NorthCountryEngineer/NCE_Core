# Independent application transfer

Core now provides a metadata-only, human-controlled transfer workflow for installed
application packages. Open **Central admin → application → Transfer or view
independent location**, or follow the transfer link on a cloud application or
installation settings page. This is additive to organization ownership adoption;
adoption does not move customer data or make an independent installation.

Two destination identity modes are admitted: `customer-core` (an independent
customer-operated Core authority) and `direct-sso` (the application uses customer
SSO directly). A receiver must implement the same capability and signed receipt
contract in either mode. Declaring an adapter does not install it or demonstrate
that it can migrate a particular application's data. Unsupported schemas,
extensions, policy semantics, identity mappings or acceptance checks block the
workflow. Core contains no arbitrary remote migration executor.

## Pairing and authority

1. An active human source organization administrator creates a pairing offer.
   Its secret is displayed once, lives in the link fragment, is hashed at rest,
   expires in ten minutes and is consumed by the first valid receiver claim.
2. Paste the original link into the destination's **Receive application** UI.
   The receiver clears the fragment immediately and generates a local Ed25519
   identity and a fresh signed descriptor. Core never opens the destination URL.
3. Return the signed claim to Core with the pairing secret. The public claim
   endpoint accepts only `{secret, envelope}`; manual copy/paste is also supported.
   A pairing secret permits pairing only: it provides no data, snapshot, export,
   activation or administration authority.
4. The source Core administrator reviews the exact destination origin, identity
   mode, adapter, key fingerprint, source IDs and capability contract. A locally
   authenticated application administrator separately supplies the source
   application's public authority descriptor. Its public key and authority
   descriptor hash are pinned in the reviewed plan. A Core session or workload
   bearer token cannot create a fresh application snapshot.

The source link is:

```
<source-origin>/application-transfer/receive/#offer_id=<uuid>&source_origin=<origin>&source_ids=<url-encoded-json>&secret=<capability>
```

`source_ids` contains `organization_id`, `installation_id`, `service_id` and a
sorted `deployment_ids` list. Opening that source link displays instructions and
clears the fragment, with no external resources or data export. Destination
implementations must perform the same immediate clearing when a link is pasted.
Do not log link fragments, request bodies, pairing secrets or customer bundles.

## Exact plan and retained metadata

Only the following input plan fields are accepted. Digests are lowercase SHA-256
hex strings; keys are canonical unpadded base64url raw Ed25519 public keys:

```json
{
  "source_adapter": {"adapter_id": "reviewed-app-v1", "public_key": "<public-key>", "authority_hash": "<sha256>"},
  "capability_contract": {
    "adapter_id": "reviewed-app-v1", "version": 1,
    "capabilities": ["fenced-cutover", "identity-mapping", "inert-import", "offline-release-verification", "paused-workers", "tenant-closure"],
    "inventory_hash": "<sha256>", "unknown_extensions": 0
  },
  "snapshot_manifest_hash": "<sha256>",
  "identity_mapping_hash": "<sha256>",
  "policy_mapping_hash": "<sha256>",
  "release_manifest_hash": "<sha256>",
  "retention_days": 30, "guest_links": "rotate", "sessions": "invalidate", "workers": "paused",
  "snapshot_reference": {"reference_id": "opaque-reference", "ciphertext_hash": "<sha256>", "key_custody": "destination"},
  "data_counts": {"records": 12, "blobs": 4}
}
```

`guest_links` can be `preserve` or `rotate`, explicitly reviewed. Count names must
be opaque entity types, never customer names or record content. Reference IDs
are inert identifiers, not URLs. Core cannot dereference a snapshot reference,
receive its decryption key, fetch a remote snapshot, or store customer bundles,
raw identity mappings or arbitrary source commands. Existing locally retained
snapshots may remain under their existing custody; a new snapshot requires a
currently authorized administrator of the hosted local application.

Core adds the exact source identity, local authorization/registration fingerprint,
receiver descriptor hash and destination descriptor to the plan. The administrator
approves that exact plan hash. Subsequent plan edits are prohibited. Changes in
source placement, clients, policy, memberships, role edges, grants or application
registration invalidate the review. Routine runtime health observations do not.
Only one active pairing/approved transfer is admitted per installation. Approved
transfers freeze new Core releases, deployment changes and integration-key issuance.

## Signed proof and manual stage acceptance

All signed documents use `nce-app-migration/v1`. An envelope has exactly
`protocol`, `purpose`, `signer_key_id`, `payload`, `signature`.
`signer_key_id` is `sha256:` followed by the SHA-256 of the raw 32-byte public key.
The signature is unpadded base64url Ed25519 over:

```
b'NCE-APP-MIGRATION-SIGNED-V1\n' + canonical_json({protocol,purpose,signer_key_id,payload})
```

Canonical JSON uses sorted keys, UTF-8, `ensure_ascii=False`, and separators
`(',', ':')`, with no floats, duplicate keys or non-JSON types. Core limits
metadata to 32 KiB and 24 levels of nesting. Verification always uses the public
key pinned by the reviewed receiver/source descriptor, not an envelope-provided
replacement. A shared fixture signed by the Proposal Studio implementation is
verified by Core's regression suite.

A receiver claim uses purpose `receiver-descriptor` and exactly:

```
{offer_id, source_ids, destination:{origin,mode,public_key,adapter_id},
 challenge, capabilities, issued_at, expires_at}
```

Origins must be valid HTTPS origins, including valid explicit ports, with no
credentials, path, query, fragment, whitespace, controls or backslashes. Times
are integer UTC epoch seconds; claims must be fresh and expire within ten minutes.
Capabilities must be the sorted exact list shown above. The source IDs and offer
ID must match the one-use offer.

A receipt uses purpose `migration-receipt` and exactly:

```
{transfer_id,plan_hash,sequence,previous_receipt_hash,stage,fence_epoch,evidence}
```

Receipts arrive at `POST /api/platform/v1/application-transfers/<uuid>/receipts/`.
They are immutable, signed and chained by SHA-256 of the previous whole envelope.
The first previous hash is empty. Identical pending submissions are idempotent;
different replacements, replay, reordering, changed plans and wrong signers fail.
Receipt intake cannot advance state. A source human administrator must inspect
and accept each exact receipt hash separately in the UI.

| Sequence | Stage and signer | Required acceptance evidence |
| --- | --- | --- |
| 1 | `snapshot`, pinned local source application key | Exact snapshot and authority hashes; local admin authorized; complete tenant closure; source writes fenced and workers paused before snapshot; ambiguous sends blocked; positive durable fence epoch |
| 2 | `staged`, pinned destination key | Exact snapshot/identity/policy/release hashes; inert import; mirrored release and offline verification; independent identity and usable local admin; rewrapped credentials; no source keys; workers paused; guest links reviewed |
| 3 | `fenced`, local source application key | Exact snapshot hash; source writes fenced; workers paused; ambiguous sends blocked; sessions invalidated; positive durable fence epoch |
| 4 | `verified`, destination key | Exact four manifest hashes; record counts and blobs match; immutable identity mapping verified; policy semantics preserved; local admin sign-in; no source dependency; restart without NCE credentials succeeds |
| 5 | `activated`, destination key | Exact snapshot hash; destination writes enabled; source fence confirmed; ambiguous sends blocked; independent operation verified |

All required acceptance flags must be literal `true`; missing, extra or false
fields block progression. The precise evidence field names live in
`platform_services/application_transfers.py::_evidence`. The first snapshot establishes a positive fence epoch; every subsequent receipt
must preserve that same epoch. Source writes and workers must be durably fenced
before creating the consistent snapshot, not after the receiver has imported it.
Unknown data/extensions require a reviewed adapter before asserting tenant closure.
Importers must create schema using approved packaged migrations and accept inert
validated records/blobs; bundles must never execute SQL, pickle, scripts, hooks,
database roles, triggers, functions or foreign servers.

Source workers, schedules, cursors and send markers are preserved paused. Ambiguous
sends are never automatically replayed. Destination identity mapping uses exact
issuer/subject identities, not email matching; unsupported authorization semantics
must fail rather than grant broader access. New issuer-scoped client registrations,
fresh consent and credential rewrapping are required. Never transfer NCE signing,
session, global directory, cipher or KMS keys. Mirror images and charts into
customer custody and verify their pinned public-key signatures offline so NCE ECR
credentials are unnecessary for restart.

Published Core charts pin full source registry references in their values schema.
Use the offline [customer chart relocation utility](helm-oci-releases.md#customer-owned-registry-relocation)
to derive a customer-key-signed chart containing the exact approved mirror
repositories with identical image digests. Preserve the publisher-signed original
chart and release alongside the derived package and signed derivation receipt.

## Handover, retention and recovery

After all five accepted stages, a separate exact-plan/final-receipt confirmation
performs the local Core handover transaction. It creates an independent placement
reference without rewriting source organization, installation, service or deployment
IDs; disables the source installation/service/cloud application; revokes source
OAuth access/refresh/grant/ID tokens, clients, application accounts and invitations;
deactivates workload credentials, agent bindings, AI query sources and federation
peers; and disables application integrations. Source runtime assignment is detached
without emitting a remote deployment/deletion command. Encrypted source connection
history is retained inactive and its retrieval is denied.

The catalog is then **reference only**. Even if an old object is held in a worker
or active flags are re-enabled in storage, reference checks deny workload, OAuth,
AI query, federation, integration, deployment and release authority. The destination
origin remains an operator-opened link, never a new polling/data-fetch target.
Other applications, organization memberships and unrelated credentials are retained.
Authorized source organization administrators can still open the transfer history
and the new independent location from the catalog. The dashboard displays the
destination link and removes deployment/key controls for that reference. Existing
human tag checks remain in force; ordinary members gain no administrative access.
No source data or infrastructure is deleted. Retention is an explicit plan input;
this workflow does not automatically expire or delete retained snapshots.

Cross-system cutover is fenced and staged: only Core's local final pointer/revocation
transaction is atomic. DNS, two databases and an identity provider cannot be claimed
as one atomic operation. The initial implementation expects a downtime/consistent
snapshot window and makes no zero-downtime promise. Cancellation is allowed before
the first snapshot receipt is accepted; after that, recovery requires an explicitly reviewed
application-specific plan. There is no automatic rollback or infrastructure retirement.

This Core implementation is the transfer coordinator, protocol intake and authority
revocation boundary. Actual data export/import, destination sign-in integration,
release installation, local authority checks, acceptance probes and durable source
fencing are supplied by application adapters. No live transfer or deployment is
evidenced merely by a configured descriptor or a locally passing Core test.
