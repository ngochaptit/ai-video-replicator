# Project Mirror V2 audit record

## Scope

This change replaces the mutable single-request local-sync exchange with a
persistent, incremental project mirror and immutable task folders. Google Drive
API transport remains on the legacy protocol and is the rollback path.

## Source snapshot

- Engine archive SHA-256: `d3ca37625029efc869dfb1465964d492ed596b1e1aae650b4b089bca419b34a4`
- Project archive SHA-256: `b8d04a3ba3b83d40c945b6d6e17c89c78d6fb52b5f0fc824e7e945b3f89ec40f`
- Engine snapshot matched the 2,234 tracked files at the audited base revision.
- The supplied project contained proposal/analyze outputs and completed footage
  batches `coarse_001` and `coarse_002`; its active `refinement_008` request was
  expired.
- Raw media was not present in the supplied project archive, so media/proxy E2E
  must be verified against the real Windows project.

## Safety properties

- Stable asset IDs derive from normalized project-relative paths.
- Public files contain no machine-local absolute paths.
- Proxies preserve full duration; unsupported video containers are transcoded to
  H.264/AAC MP4 and duration drift beyond 120 ms is rejected.
- Asset changes increment the generation and emit explicit invalidation records;
  deletions emit tombstones.
- Task responses are size-bounded strict JSON, identity-bound, schema-validated,
  asset/hash-bound, and timestamp-range-checked.
- Receipts and apply markers are immutable and make duplicate consumption safe.
- Migration only inventories and hashes V1 state. It is idempotent and does not
  rewrite the legacy request, response, bridge state, or footage progress.

## Rollback

Set `exchange_protocol` to `legacy`. No squash, data conversion, or destructive
cleanup is required. V2 data stays under `.moon/project-mirror-v2`,
`.moon/tasks-v2`, and `MON_EDIT/projects/<project_id>`.
