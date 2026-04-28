"""
Utility functions for gRPC transaction processing — adapted from arbito/grpc_utils.py
"""

import base58
from constants import JUPITER_PROGRAMS, KNOWN_BOT_WALLETS, JITO_TIP_ACCOUNTS


def extract_signer(transaction) -> str:
    """Extract the first signer (account key) from transaction as string"""
    try:
        if hasattr(transaction, 'transaction'):
            tx = transaction.transaction
        else:
            tx = transaction

        message = tx.message
        for account_key in message.account_keys:
            return base58.b58encode(account_key).decode('utf-8')
    except Exception:
        pass
    return ""


def should_skip_transaction(signer: str) -> bool:
    """Check if transaction should be skipped based on filtering criteria"""
    return signer in KNOWN_BOT_WALLETS


def extract_addresses(transaction, meta) -> list[str]:
    """Extract all account keys from transaction as strings"""
    addresses = []

    if hasattr(transaction, 'transaction'):
        tx = transaction.transaction
    else:
        tx = transaction

    message = tx.message
    for account_key in message.account_keys:
        addresses.append(base58.b58encode(account_key).decode('utf-8'))

    for loaded_writable in meta.loaded_writable_addresses:
        addresses.append(base58.b58encode(loaded_writable).decode('utf-8'))

    for loaded_readonly in meta.loaded_readonly_addresses:
        addresses.append(base58.b58encode(loaded_readonly).decode('utf-8'))

    return addresses


def contains_jito_tip_account(addresses: list) -> bool:
    """Check if any address in the list is a Jito tip account"""
    for address in addresses:
        if address in JITO_TIP_ACCOUNTS:
            return True
    return False
