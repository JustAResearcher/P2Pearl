"""Build the P2Pearl coinbase — feeless decentralized payout + sidechain commitment.

Given the deterministic PPLNS payout list (``p2pearl.consensus.pplns``) and the id
of the ShareBlock being created, produce the coinbase output set:

    [ TxOutput(grains_i, P2TR(addr_i)) for each payout ]     # pays every recent miner directly
    + TxOutput(0, OP_RETURN <share_id>)                       # commits this sidechain block
    + TxOutput(0, OP_RETURN aa21a9ed<witness_commitment>)     # segwit commitment (added at assembly)

The ``OP_RETURN <share_id>`` is the merge-mining commitment: because it lands in
the transaction merkle root, it seeds the Pearlhash work and binds the ZK proof,
so a single PoW solution ratifies the sidechain. Pearl consensus
(pearl/node/blockchain/validate.go:228) permits only P2TR / P2MR / OP_RETURN
coinbase outputs and requires ``sum(outputs) <= coinbasevalue`` — both satisfied.

bitcoinutils is imported lazily so the rest of the package (and the unit tests)
import without it. The script/scriptSig construction is generalized from the Pearl
gateway's ``create_coinbase_transaction`` (which only supports a single payout
output) to the many-output case P2Pool needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..consensus.pplns import Payout


@dataclass(frozen=True)
class CoinbaseOutput:
    value: int
    address: str | None = None          # P2TR payout; mutually exclusive with op_return_data
    op_return_data: bytes | None = None  # raw bytes for an OP_RETURN output


def build_coinbase_outputs(
    payouts: list[Payout],
    share_id: bytes,
    coinbase_value: int,
) -> list[CoinbaseOutput]:
    """Ordered coinbase outputs: PPLNS payouts (as given) + the sidechain commitment.

    ``payouts`` is assumed already deterministically ordered (it is, coming from
    ``compute_pplns_payouts``). The OP_RETURN witness-commitment output is added
    later by ``assemble_coinbase_tx``.
    """
    if len(share_id) != 32:
        raise ValueError("share_id must be 32 bytes")
    total = sum(p.grains for p in payouts)
    if total > coinbase_value:
        raise ValueError(f"payouts ({total}) exceed coinbase value ({coinbase_value})")
    outs = [CoinbaseOutput(p.grains, address=p.address) for p in payouts]
    outs.append(CoinbaseOutput(0, op_return_data=share_id))
    return outs


def assemble_coinbase_tx(
    outputs: list[CoinbaseOutput],
    height: int,
    coinbase_aux: dict[str, str] | None = None,
    default_witness_commitment: str | None = None,
):
    """Assemble a ``bitcoinutils`` coinbase Transaction (lazy import).

    Generalized from the Pearl gateway's create_coinbase_transaction to many
    outputs + OP_RETURN commitments. scriptSig carries BIP34 height + an extra
    nonce byte + aux flags, matching the node.
    """
    from bitcoinutils.script import Script
    from bitcoinutils.transactions import Transaction, TxInput, TxOutput, TxWitnessInput

    height_script_hex = Script([height]).to_hex()
    coinbase_script = bytes.fromhex(height_script_hex) + b"\x00"
    if coinbase_aux and "flags" in coinbase_aux:
        coinbase_script += bytes.fromhex(coinbase_aux["flags"])

    cb_in = TxInput(
        txid="0" * 64,
        txout_index=0xFFFFFFFF,
        script_sig=Script([coinbase_script.hex()]),
        sequence=b"\xff\xff\xff\xff",
    )

    tx_outs = [TxOutput(o.value, _script_for(o)) for o in outputs]

    has_witness = default_witness_commitment is not None
    if has_witness:
        tx_outs.append(
            TxOutput(0, Script(["OP_RETURN", "aa21a9ed" + default_witness_commitment]))
        )

    return Transaction(
        inputs=[cb_in],
        outputs=tx_outs,
        locktime=b"\x00\x00\x00\x00",
        version=b"\x01\x00\x00\x00",
        has_segwit=has_witness,
        witnesses=[TxWitnessInput([bytes(32).hex()])] if has_witness else None,
    )


def _script_for(out: CoinbaseOutput):
    from bitcoinutils.script import Script

    if out.op_return_data is not None:
        return Script(["OP_RETURN", out.op_return_data.hex()])
    if out.address is None:
        raise ValueError("output has neither address nor op_return_data")
    return _p2tr_script(out.address)


def _p2tr_script(address: str):
    """``OP_1 <32-byte program>`` from a bech32m P2TR address (any HRP).

    Mirrors the gateway's get_script_pubkey_from_p2tr_address.
    """
    from bitcoinutils.bech32 import Encoding, bech32_decode, convertbits
    from bitcoinutils.script import Script

    hrp, data, encoding = bech32_decode(address)
    if hrp is None or not data:
        raise ValueError("invalid bech32m address")
    if data[0] != 1:
        raise ValueError("expected taproot witness version 1")
    if encoding != Encoding.BECH32M:
        raise ValueError("taproot address must be bech32m")
    program = convertbits(data[1:], 5, 8, False)
    if program is None or len(program) != 32:
        raise ValueError("taproot witness program must be 32 bytes")
    return Script.from_raw((b"\x51\x20" + bytes(program)).hex())
