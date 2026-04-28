"""Round-trip encoding tests for manifest.py ix builders.

Validates the byte-level layout against the spec, using known-good worked
examples from the Manifest source. No on-chain interaction.
"""
from solders.pubkey import Pubkey

import manifest as m


def test_claim_seat():
    payer = Pubkey.from_string("Dx4wVQL1ZofsypesxS96mga2uhygLwGRwVc4egju4tq5")
    market = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")  # jupSOL/SOL
    ix = m.build_claim_seat_ix(payer, market)
    assert ix.program_id == m.PROGRAM_ID
    assert ix.data == b"\x01"
    assert len(ix.accounts) == 3
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[1].is_writable and not ix.accounts[1].is_signer
    assert not ix.accounts[2].is_writable
    print("  claim_seat: 1-byte data, 3 accounts ✓")


def test_post_only_sell_0_5_sol_at_150():
    """Worked example from manifest_spec.md: sell 0.5 SOL at $150 on SOL/USDC."""
    payer = Pubkey.from_string("Dx4wVQL1ZofsypesxS96mga2uhygLwGRwVc4egju4tq5")
    market = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

    # Spec example: mantissa=15, exponent=-2, base_atoms=500_000_000
    # Verify the price encoder produces a price equivalent to 15×10^-2 = 0.15
    mantissa, exponent = m.encode_price(human_price=150.0, base_decimals=9, quote_decimals=6)
    encoded_atom_price = mantissa * (10 ** exponent)
    expected = 0.15  # 0.15 quote_atoms (USDC microcents) per base atom (SOL lamport)
    assert abs(encoded_atom_price - expected) / expected < 1e-9, (
        f"price encoder off: got {encoded_atom_price}, want {expected}"
    )
    print(f"  price encoder: 150 USD/SOL → mantissa={mantissa} exp={exponent} "
          f"(= {encoded_atom_price} quote-atoms/base-atom) ✓")

    order = m.PlaceOrderParams(
        base_atoms=500_000_000,           # 0.5 SOL
        price_mantissa=15,                # spec example
        price_exponent=-2,
        is_bid=False,                     # sell
        last_valid_slot=m.NO_EXPIRATION,
        order_type=m.OrderType.POST_ONLY,
    )
    packed = order.pack()
    # Borsh encoding, no repr/padding: 8+4+1+1+4+1 = 19 bytes.
    # (Initial spec said 20, but the Rust struct has no #[repr] — verified
    #  against programs/manifest/src/program/processor/batch_update.rs.)
    assert len(packed) == 19, f"PlaceOrderParams must be 19 bytes, got {len(packed)}"

    # Field-by-field verify (little-endian)
    import struct as _s
    base_atoms, mant, exp, is_bid, slot, otype = _s.unpack("<QIbBIB", packed)
    assert base_atoms == 500_000_000
    assert mant == 15
    assert exp == -2
    assert is_bid == 0  # ask
    assert slot == 0
    assert otype == 2  # PostOnly
    print(f"  PlaceOrderParams: 19-byte Borsh layout ✓ ({packed.hex()})")

    ix = m.build_batch_update_ix(payer, market, orders=[order])
    # ix data: disc(1) + option_None(1) + cancels_len(4) + orders_len(4) + order(19) = 29
    assert ix.data[0] == m.DISC_BATCH_UPDATE
    assert ix.data[1] == 0  # trader_index_hint = None
    assert _s.unpack("<I", ix.data[2:6])[0] == 0  # 0 cancels
    assert _s.unpack("<I", ix.data[6:10])[0] == 1  # 1 order
    assert ix.data[10:29] == packed
    assert len(ix.data) == 29
    print(f"  BatchUpdate(1 order, 0 cancels): 29-byte ix data ✓")


def test_batch_update_cancel_with_hint():
    payer = Pubkey.from_string("Dx4wVQL1ZofsypesxS96mga2uhygLwGRwVc4egju4tq5")
    market = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")

    cancel = m.CancelOrderParams(order_sequence_number=42, order_index_hint=336)
    packed = cancel.pack()
    # u64 seq + 1 byte tag + u32 hint = 13 bytes
    assert len(packed) == 13, f"CancelOrderParams with hint must be 13 bytes, got {len(packed)}"

    cancel_no_hint = m.CancelOrderParams(order_sequence_number=42)
    packed_no_hint = cancel_no_hint.pack()
    # u64 seq + 1 byte tag = 9 bytes
    assert len(packed_no_hint) == 9
    print(f"  CancelOrderParams: with hint=13B, without hint=9B ✓")

    ix = m.build_batch_update_ix(payer, market, cancels=[cancel])
    # disc(1) + coption(1) + cancels_len(4) + cancel(13) + orders_len(4) = 23
    assert len(ix.data) == 23
    print(f"  BatchUpdate(1 cancel): 23-byte ix data ✓")


def test_deposit_withdraw_layout():
    payer = Pubkey.from_string("Dx4wVQL1ZofsypesxS96mga2uhygLwGRwVc4egju4tq5")
    market = Pubkey.from_string("8iC3HzYGW6ji6chaxRvNoBeG3uLgQZUPNL5R7RmM8uQv")
    ata = Pubkey.from_string("So11111111111111111111111111111111111111112")
    vault = Pubkey.from_string("So11111111111111111111111111111111111111112")
    mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

    dep = m.build_deposit_ix(payer, market, ata, vault, mint, amount_atoms=1_000_000_000)
    # disc(1) + u64(8) + coption_None(1) = 10 bytes
    assert dep.data[0] == m.DISC_DEPOSIT
    assert len(dep.data) == 10
    assert len(dep.accounts) == 6
    print(f"  Deposit: 10B data, 6 accounts ✓")

    wd = m.build_withdraw_ix(payer, market, ata, vault, mint, amount_atoms=500_000_000)
    assert wd.data[0] == m.DISC_WITHDRAW
    assert len(wd.data) == 10
    print(f"  Withdraw: 10B data, 6 accounts (same shape as Deposit) ✓")


def test_lst_pricing_realistic():
    """LSTs trade at roughly 1:1 with SOL — verify the encoder handles
    near-unity ratios (e.g. jupSOL/SOL where 1 jupSOL = 1.04 SOL)."""
    # both decimals=9, price=1.04 SOL/jupSOL
    mant, exp = m.encode_price(human_price=1.04, base_decimals=9, quote_decimals=9)
    val = mant * (10 ** exp)
    assert abs(val - 1.04) / 1.04 < 1e-9, f"got {val}, want 1.04"
    print(f"  LST pair 1.04: mantissa={mant} exp={exp} → {val} ✓")

    # near-1.0 ratio with high precision (bSOL/SOL)
    mant, exp = m.encode_price(human_price=1.0123456, base_decimals=9, quote_decimals=9)
    val = mant * (10 ** exp)
    assert abs(val - 1.0123456) / 1.0123456 < 1e-7, f"got {val}, want 1.0123456"
    print(f"  LST pair 1.0123456: mantissa={mant} exp={exp} → {val} ✓")


if __name__ == "__main__":
    print("manifest.py encoding tests:")
    test_claim_seat()
    test_post_only_sell_0_5_sol_at_150()
    test_batch_update_cancel_with_hint()
    test_deposit_withdraw_layout()
    test_lst_pricing_realistic()
    print("all ok")
