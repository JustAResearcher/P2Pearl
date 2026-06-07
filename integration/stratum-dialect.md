# Stratum dialect — what P2Pearl serves, and why it's tolerant

P2Pearl's stratum server must let the production **SRBMiner `--algorithm pearlhash`**
fleet connect with no miner changes. There are **two incompatible Pearlhash stratum
dialects** in the Pearl ecosystem:

| | object (LuckyPool / Herominers) | positional (alphapool) |
|---|---|---|
| handshake | **authorize-first** (no subscribe/configure) | configure -> subscribe -> authorize |
| params | JSON **objects** | JSON **arrays** |
| `mining.authorize` | `{wallet, worker, agent}` | `["wallet.worker", password]` |
| `mining.notify` | `{job_id, header(hex), target(256-bit hex), height}` | `[job_id, prevhash, header, seq, ntime, nbits, clean]` |
| `mining.submit` | `{job_id, plain_proof(b64), hs}` | `[worker, job_id, plain_proof(b64)]` |

## What SRBMiner speaks — INFERRED, not captured

The production fleet runs SRBMiner against `us2.pearl.herominers.com:1200` with **0
rejects**, and Herominers is classified as the **object** dialect everywhere in the
Pearl tree (`pearl-stratum/.../luckypool_client.py`). If SRBMiner spoke the positional
dialect it could not mine there. So SRBMiner almost certainly speaks the **object**
dialect. **Caveat:** there is no direct byte-capture of SRBMiner in the source tree —
the only Pearl stratum captures (`C:/Source/bridge/captures/`) are alpha-miner /
alphapool. The object-dialect choice is the best available inference.

## What P2Pearl's server does

`stratum/server.py` is **dialect-tolerant**, mirroring the proven
`C:/Source/bridge/cmd/bridge/downstream.go` (which played pool to real Pearl miners,
11/11 shares accepted):

- Accepts **authorize-first** AND configure/subscribe-first handshakes.
- Parses **both** `mining.authorize` and `mining.submit` param shapes (object or array).
- Detects the dialect from the client's messages and **mirrors it** in `mining.notify`;
  **defaults to object**.
- Never sends `pearl.challenge` (a public-pool anti-DoS gate; P2Pool handles Sybil at
  the P2P layer, not stratum).
- Tolerantly acks unknown methods so a miner sending extra methods still connects.

## Before production: capture to confirm

Point SRBMiner at the in-tree 1:1 logging proxy `C:/Source/bridge/cmd/logproxy` ->
Herominers and diff one handshake to nail down the open uncertainties:

1. Does SRBMiner send `mining.subscribe`/`mining.configure`, or authorize-first?
2. `mining.notify` `header` encoding — **hex** vs base64.
3. Share `target` endianness — **big-endian** vs little-endian.

The gate-tested `luckypool_client.py` uses a **hex** header and a **big-endian** 256-bit
`target`, which is what P2Pearl emits by default. If a capture disagrees, adjust
`StratumJob.notify_params` / `target_hex` in `stratum/server.py`.
