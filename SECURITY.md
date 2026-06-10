# Security

## Reporting

Found a vulnerability? Open a private security advisory on GitHub, or contact the
maintainer — please don't file a public issue for anything exploitable.

## P2P threat model

A P2Pearl node accepts connections from untrusted peers, so the gossip layer
(`src/p2pearl/p2p/node.py`) treats every peer as potentially hostile. Protections:

- **Forged shares are impossible.** Every gossiped share carries a Pearlhash
  proof that is verified against the consensus share target before the share is
  added or relayed, and its PPLNS payout set is recomputed from the verifier's
  own sharechain (a peer cannot fake the reward split, the difficulty, the
  coinbase value, or the proof-of-work). Consensus-invalid shares are rejected
  *before* their large proof is even fetched.
- **Bounded memory.** The on-demand proof cache, the seen-block set, and the
  pending-announce map are all size-capped; oversized messages drop the peer.
- **Anti-flood / anti-amplification.** The expensive request handlers
  (`getshares`, `getproof`, `block`, `hello`) are rate-limited per peer in a
  fixed window; a peer that keeps exceeding the cap is dropped. Block relays are
  deduplicated by hash (no broadcast storms) and size-capped. Outbound sends are
  backpressured by the async transport, so a slow/stuck reader cannot make the
  node buffer unbounded data.

These mirror the hardening in Monero P2Pool's June 2026 P2P-server updates
(message-flood limits, peer-request caps, block-broadcast dedup, write-queue
bounds). P2Pearl has no remote console/command port, so that class of P2Pool
issue does not apply here.

## Operator hygiene

- Keep the `pearld` JSON-RPC port (44107/44109) bound to localhost; never expose
  it publicly. The managed pearld does this for you.
- The stratum port (3360) and P2P port (37900) are the only ports you forward.
