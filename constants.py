"""
Constants for arbitrage tracker — subset of arbito/constants.py
Read-only analysis: no KLend, no LUTs, no trading thresholds, no wallet ATAs
"""

# DEX Protocol Names
RAYDIUM_AMM_V4 = "Raydium_AMM_v4"
WHIRLPOOL = "Whirlpool"
METEORA_DLMM = "Meteora_DLMM"
METEORA_DAMM = "Meteora_DAMM"
PUMPSWAP = "PumpSwap"
RAYDIUM_CPMM = "Raydium_CPMM"
RAYDIUM_CLMM = "Raydium_CLMM"

# Program IDs
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
METEORA_DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_DAMM_PROGRAM = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"
PUMPSWAP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_CPMM_PROGRAM_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CLMM_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

# Core Solana Program IDs
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Additional DEX Program IDs
ORCA_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
JUPITER_V4_PROGRAM = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"
SERUM_V3_PROGRAM = "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"
SERUM_V2_PROGRAM = "EUqojwWA2rd19FZrzeBncJsm38Jm1hEhE3zsmX3bRc2o"
SERUM_V1_PROGRAM = "BJ3jrUzddfuSrZHXSCxMUUQsjKEyLmuuyZebkcaFp2fg"
PHOENIX_PROGRAM = "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY"
OPENBOOK_PROGRAM = "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb"
ORCA_V1_PROGRAM = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
ORCA_V2_PROGRAM = "DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1"

# Jupiter aggregator programs
JUPITER_PROGRAMS = {
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter v4
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUP3c2Uh3WA4Ng34tw6kPd2G4C5BB21Xo36Je1s32Ph",  # Jupiter v3
    "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uJvfo",  # Jupiter v2
}

SUPPORTED_DEXS = [RAYDIUM_AMM_V4, METEORA_DLMM, WHIRLPOOL, PUMPSWAP, RAYDIUM_CPMM, RAYDIUM_CLMM]
SUPPORTED_PROGRAMS = [RAYDIUM_AMM_V4_PROGRAM, METEORA_DLMM_PROGRAM, WHIRLPOOL_PROGRAM, PUMPSWAP_PROGRAM_ID, RAYDIUM_CPMM_PROGRAM_ID, RAYDIUM_CLMM_PROGRAM_ID]

# Mapping of program IDs to human-readable DEX names
DEX_PROGRAMS = {
    RAYDIUM_AMM_V4_PROGRAM: RAYDIUM_AMM_V4,
    WHIRLPOOL_PROGRAM: WHIRLPOOL,
    ORCA_CLMM_PROGRAM: "Orca CLMM",
    JUPITER_V6_PROGRAM: "Jupiter V6",
    JUPITER_V4_PROGRAM: "Jupiter V4",
    ORCA_V1_PROGRAM: "Orca V1",
    ORCA_V2_PROGRAM: "Orca V2",
    "FarmqiPv5eAj3j1GMdMCMUGXqPUvmquZtMDY4vi58q": "Raydium Farm",
    "EhYXQPv5vpGfYQDtMDDp9rMFXYamQGK7j6wnX6LdNEt7": "Meteora",
    "SSwpkEEcbUqx4vtoEByFjSkhKdCT862DNVb52nZg1UZ": "Step Finance",
    "AMM55ShdkoGRB5jVYPjWziwk8m5MpwyDgsMWHaMSQWH6": "Aldrin AMM",
    "DSwpgjMvXhtGn6BsbqmacdBZyfLj6jSWf3HJvGHmwkpt": "Delta Fi",
    "BSwp6bEBihVLdqJRKS58MWmqVZ1YMJLP2v8pvdWyL1dd": "BStep",
    "HyaB3W9q6XdA5xwpU4XnSZV94htfmbmqJXZcEbRaJutt": "Hyperspace",
    PHOENIX_PROGRAM: "Phoenix",
    OPENBOOK_PROGRAM: "OpenBook",
    SERUM_V3_PROGRAM: "Serum V3",
    SERUM_V2_PROGRAM: "Serum V2",
    SERUM_V1_PROGRAM: "Serum V1",
    "2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c": "DEX Aggregator",
    "SwaPpA9LAaLfeLi3a68M4DjnLqgtticKg6CnyNwgAC8": "Swap Program",
    PUMPSWAP_PROGRAM_ID: PUMPSWAP,
    RAYDIUM_CPMM_PROGRAM_ID: RAYDIUM_CPMM,
    RAYDIUM_CLMM_PROGRAM_ID: RAYDIUM_CLMM,
    METEORA_DLMM_PROGRAM: METEORA_DLMM,
    METEORA_DAMM_PROGRAM: METEORA_DAMM,
    "metaX99LHn3A7Gr7VAcCfXhpfocvpMpqQ3eyp3PGUUq": "Meteora",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s": "Meteora Pools",
    "24Uqj9JCLxUeoC3hGfh5W3s9FM9uCHDS2SG3LYwBpyTi": "Meteora Stable",
    "3xxgYc3jXPdjqpMdrGXYDqkJVGqMbUxSbePDdHpWbDYj": "Meteora Multi",
}

# Common system/program accounts to ignore in pool heuristics
SYSTEM_ACCOUNTS = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Stake11111111111111111111111111111111111111",
    "Vote111111111111111111111111111111111111111",
    "Config1111111111111111111111111111111111111",
    "Ed25519SigVerify111111111111111111111111111",
    "KeccakSecp256k11111111111111111111111111111",
    "BPFLoaderUpgradeab1e11111111111111111111111",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    "SwaPpA9LAaLfeLi3a68M4DjnLqgtticKg6CnyNwgAC8",
    "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy",
}

# Known swap instruction discriminators (first 8 bytes of instruction data)
SWAP_DISCRIMINATORS = {
    # Raydium
    b"\x09": f"{RAYDIUM_AMM_V4} Swap",
    # Orca Whirlpool
    b"\xf8\xc6\x9e\x91\xe1\x75\x87\xc8": f"{WHIRLPOOL} Swap",
    b"\x2e\x9c\xf3\x76\x0d\xcd\xfb\xb2": f"{WHIRLPOOL} Add Liquidity",
    b"\xa0\x26\xd0\x6f\x68\x5b\x2c\x01": f"{WHIRLPOOL} Remove Liquidity",
    b"\x85\x1d\x59\xdf\x45\xee\xb0\x0a": f"{WHIRLPOOL} Add Liquidity",
    b"\x3a\x7f\xbc\x3e\x4f\x52\xc4\x60": f"{WHIRLPOOL} Remove Liquidity",
    # Jupiter
    b"\xb4\x0e\xc7\x8b\xe8\x49\x91\x26": "Jupiter Route",
    # PumpSwap
    b"\x66\x06\x3d\x12\x01\xda\xeb\xea": f"{PUMPSWAP} Buy",
    b"\x38\xfc\x74\x08\x9e\xdf\xcd\x5f": f"{PUMPSWAP} Buy Exact SOL In",
    b"\x33\xe6\x85\xa4\x01\x7f\x83\xad": f"{PUMPSWAP} Sell",
    # Raydium CPMM
    b"\x8f\xbe\x5a\xda\xc4\x1e\x33\xde": f"{RAYDIUM_CPMM} Swap Base Input",
    b"\x37\xd9\x62\x56\xa3\x4a\xb4\xad": f"{RAYDIUM_CPMM} Swap Base Output",
    # Meteora DLMM
    b"\x09\xc1\x8a\x2d\x5d\x56\x3c\x80": f"{METEORA_DLMM} Swap",
    b"\x84\x3a\x21\x5c\x58\x8a\x91\x4f": f"{METEORA_DLMM} Add Liquidity",
    b"\xa8\x15\x69\x87\x42\x9c\x3a\x1d": f"{METEORA_DLMM} Remove Liquidity",
    b"\x2e\x1d\xc8\x46\x51\x9f\xa8\xb3": f"{METEORA_DLMM} Swap",
}

# Statalyzer Address Lookup Table — run create_lut.py once to populate
STATALYZER_LUT_ADDRESS = ""  # fill in after running create_lut.py

# Lunar Lander tip accounts (Hello Moon transaction landing service)
LUNAR_LANDER_TIP_ACCOUNTS = [
    "moon17L6BgxXRX5uHKudAmqVF96xia9h8ygcmG2sL3F",
    "moon26Sek222Md7ZydcAGxoKG832DK36CkLrS3PQY4c",
    "moon7fwyajcVstMoBnVy7UBcTx87SBtNoGGAaH2Cb8V",
    "moonBtH9HvLHjLqi9ivyrMVKgFUsSfrz9BwQ9khhn1u",
    "moonCJg8476LNFLptX1qrK8PdRsA1HD1R6XWyu9MB93",
    "moonF2sz7qwAtdETnrgxNbjonnhGGjd6r4W4UC9284s",
    "moonKfftMiGSak3cezvhEqvkPSzwrmQxQHXuspC96yj",
    "moonQBUKBpkifLcTd78bfxxt4PYLwmJ5admLW6cBBs8",
    "moonXwpKwoVkMegt5Bc776cSW793X1irL5hHV1vJ3JA",
    "moonZ6u9E2fgk6eWd82621eLPHt9zuJuYECXAYjMY1C",
]

# Jito tip accounts
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

# Known bot wallets
KNOWN_BOT_WALLETS = {
    "MEViEnscUm6tsQRoGd9h6nLQaQspKj7DB2M5FwM3Xvz",
    "9973hWbcumZNeKd4UxW1wT892rcdHQNwjfnz8KwzyWp6",
    "Ai4zqY7gjyAPhtUsGnCfabM5oHcZLt3htjpSoUKvxkkt",
    "BCbrp5VcG6vgYz3ib1VfwEBy1T8ivj1i4WVNUY4vi58q",
    "5CcLBq4j7iQbhfocd63VafV2XBpEcWeF8383Ux6Lbxbt",
    "GHPCChGqtKf4sFaN1wPPCapcweKXBBngB3hF7D6nT29e",
    "JD1dHSqYkrXvqUVL8s6gzL1yB7kpYymsHfwsGxgwp55h",
    "JD25qVdtd65FoiXNmR89JjmoJdYk9sjYQeSTZAALFiMy",
    "JD38n7ynKYcgPpF7k1BhXEeREu1KqptU93fVGy3S624k",
    "7rhxnLV8C77o6d8oz26AgK8x8m5ePsdeRawjqvojbjnQ",
    "Lucky73WpiBVVgnZm8458en4EwR5eg8hP18oCjkaMUZ",
    "HodL7w84ZMX29GEmNVZMno2o2oFQzNWuXG5ovQKsP8WFNd",
    "77777T2qnynHFsA63FyfY766ciBTXizavU1f5HeZXwN",
    "96vF7CBEESyk2mKSxBhEtbzxkUXvhxVt1mViLzrsow5r",
    "2gLZGf5gFDqujEX2axoS86MJt5Fw2jtJbMz9QAZPC3o8",
    "4Ekt26mxNBbPQehFqXNSKm5VfZgtMyYaDjg8fAg6vsar",
    "6666ZVaB72aoJYakpsr8GaYBWaHL8kxkqMDjPiUNGQgG",
    "SJcb8WFfTvagHshWLP5zT4QPk5vUxiDED88ys2LXwuT",
    "FEnAtqzhguaqSwid5RWTRLqyZGcsVpnjs85wwjZp596o",
    "67EbFB6knmgMwa1bcyRCwL2bQUfQEo3BYXHTjG7cXisa",
}

# Well-Known Token Mint Addresses
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USDH_MINT = "USDH1SM1ojwWUga67PGrgFWUHibbjqMvuMaDkRJTgkX"
ETH_MINT = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
BTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"
RAY_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
MSOL_MINT = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"
STSOL_MINT = "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj"
JTO_MINT = "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL"
JITOSOL_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
BSOL_MINT = "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"
JUPSOL_MINT = "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"

# Additional established tokens
WIF_MINT = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
PYTH_MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
ORCA_MINT = "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"
WEN_MINT = "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk"
RENDER_MINT = "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof"
HNT_MINT = "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux"
TRUMP_MINT = "WLFinEv6ypjkczcS83FZqFpgFZYwQXutRbxGe7oC16g"
FARTCOIN_MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
MEW_MINT = "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5"
AI16Z_MINT = "HeLp6NuQkmYB4pYWo2zYs22mESHXPQYzXbB8n4V98jwC"
POPCAT_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
DRIFT_MINT = "DriFtupJYLTosbwoN8koMbEYSx54aFAVLddWsbksjwg7"
KMNO_MINT = "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS"
TNSR_MINT = "TNSRxcUxoT9xBG3de7PiJyTDYu7kskLqcpddxnEJAS6"
W_MINT = "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ"
INF_MINT = "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm"
VSOL_MINT = "vSoLxydx6akxyMD9XEcPvGYNGq6Nn66oqVb3UkGkei7"
DSOL_MINT = "Dso1bDeDjCQxTrWHqUUi63oBvV7Mdm6WaobLbQ7gnPQ"
EDGESOL_MINT = "edge86g9cVz87xcpKpy3J77vbp4wYd9idEV562CCntt"
BONKSOL_MINT = "BonK1YhkXEGLZzwtcvRTip3gAL9nCeQD7ppZBLXhtTs"
BOME_MINT = "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82"
GOAT_MINT = "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump"
PENGU_MINT = "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"
PNUT_MINT = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
ACT_MINT = "GJAFwWjJ3vnTsrQVabjBVK2TYB1YtRCQXRDfDgUnpump"

# Well-known token metadata
WELL_KNOWN_TOKENS = {
    SOL_MINT: {"symbol": "SOL", "name": "Solana", "decimals": 9},
    USDC_MINT: {"symbol": "USDC", "name": "USD Coin", "decimals": 6},
    USDT_MINT: {"symbol": "USDT", "name": "Tether USD", "decimals": 6},
    USDH_MINT: {"symbol": "USDH", "name": "USDH", "decimals": 6},
    ETH_MINT: {"symbol": "ETH", "name": "Ethereum", "decimals": 8},
    BTC_MINT: {"symbol": "BTC", "name": "Bitcoin", "decimals": 6},
    RAY_MINT: {"symbol": "RAY", "name": "Raydium Token", "decimals": 6},
    BONK_MINT: {"symbol": "BONK", "name": "Bonk", "decimals": 5},
    MSOL_MINT: {"symbol": "mSOL", "name": "Marinade SOL", "decimals": 9},
    STSOL_MINT: {"symbol": "stSOL", "name": "Lido Staked SOL", "decimals": 9},
    JTO_MINT: {"symbol": "JTO", "name": "Jito", "decimals": 9},
    JITOSOL_MINT: {"symbol": "jitoSOL", "name": "Jito Staked SOL", "decimals": 9},
    BSOL_MINT: {"symbol": "bSOL", "name": "Blaze SOL", "decimals": 9},
    JUPSOL_MINT: {"symbol": "jupSOL", "name": "Jupiter SOL", "decimals": 9},
    WIF_MINT: {"symbol": "WIF", "name": "dogwifhat", "decimals": 6},
    JUP_MINT: {"symbol": "JUP", "name": "Jupiter", "decimals": 6},
    PYTH_MINT: {"symbol": "PYTH", "name": "Pyth Network", "decimals": 6},
    ORCA_MINT: {"symbol": "ORCA", "name": "Orca", "decimals": 6},
    WEN_MINT: {"symbol": "WEN", "name": "Wen", "decimals": 5},
    RENDER_MINT: {"symbol": "RENDER", "name": "Render", "decimals": 8},
    HNT_MINT: {"symbol": "HNT", "name": "Helium", "decimals": 8},
    TRUMP_MINT: {"symbol": "TRUMP", "name": "Official Trump", "decimals": 6},
    FARTCOIN_MINT: {"symbol": "FARTCOIN", "name": "Fartcoin", "decimals": 6},
    MEW_MINT: {"symbol": "MEW", "name": "cat in a dogs world", "decimals": 5},
    AI16Z_MINT: {"symbol": "ai16z", "name": "ai16z", "decimals": 9},
    POPCAT_MINT: {"symbol": "POPCAT", "name": "Popcat", "decimals": 9},
    DRIFT_MINT: {"symbol": "DRIFT", "name": "Drift Protocol", "decimals": 6},
    KMNO_MINT: {"symbol": "KMNO", "name": "Kamino", "decimals": 6},
    TNSR_MINT: {"symbol": "TNSR", "name": "Tensor", "decimals": 9},
    W_MINT: {"symbol": "W", "name": "Wormhole", "decimals": 6},
    INF_MINT: {"symbol": "INF", "name": "Sanctum Infinity", "decimals": 9},
    VSOL_MINT: {"symbol": "vSOL", "name": "The Vault SOL", "decimals": 9},
    DSOL_MINT: {"symbol": "dSOL", "name": "Drift Staked SOL", "decimals": 9},
    EDGESOL_MINT: {"symbol": "edgeSOL", "name": "Edgevana SOL", "decimals": 9},
    BONKSOL_MINT: {"symbol": "bonkSOL", "name": "Bonk SOL", "decimals": 9},
    BOME_MINT: {"symbol": "BOME", "name": "Book of Meme", "decimals": 6},
    GOAT_MINT: {"symbol": "GOAT", "name": "Goatseus Maximus", "decimals": 6},
    PENGU_MINT: {"symbol": "PENGU", "name": "Pudgy Penguins", "decimals": 6},
    PNUT_MINT: {"symbol": "PNUT", "name": "Peanut the Squirrel", "decimals": 6},
    ACT_MINT: {"symbol": "ACT", "name": "Act I The AI Prophecy", "decimals": 6},
}

# Stablecoin mints (for quote-token normalization)
STABLECOIN_MINTS = {USDC_MINT, USDT_MINT, USDH_MINT}

# Liquidity tiers for per-token concentration limits.
# Multiplier applied to base max_per_token setting.
# Tier 1 (3.0x): Deep liquidity, high market cap — SOL, BTC, ETH, stablecoins
# Tier 2 (2.0x): Large cap, good liquidity — LSTs, major memecoins, DeFi blue chips
# Tier 3 (1.0x): Mid cap — smaller memecoins, newer tokens (default)
TOKEN_LIQUIDITY_TIER = {
    # Tier 1: 3x concentration allowed
    SOL_MINT: 3.0,
    BTC_MINT: 3.0,
    ETH_MINT: 3.0,
    USDC_MINT: 3.0,
    USDT_MINT: 3.0,
    # Tier 2: 2x concentration allowed
    MSOL_MINT: 2.0,
    STSOL_MINT: 2.0,
    JITOSOL_MINT: 2.0,
    BSOL_MINT: 2.0,
    JUPSOL_MINT: 2.0,
    WIF_MINT: 2.0,
    BONK_MINT: 2.0,
    JUP_MINT: 2.0,
    RAY_MINT: 2.0,
    JTO_MINT: 2.0,
    RENDER_MINT: 2.0,
    TRUMP_MINT: 2.0,
    # Tier 3 (1.0x): everything else — default, no entry needed
}

# Quote token priority for price normalization (most preferred first)
QUOTE_PRIORITY = [USDC_MINT, USDT_MINT, USDH_MINT, SOL_MINT]

# URLs
SOLSCAN_TX_BASE_URL = "https://solscan.io/tx/"
