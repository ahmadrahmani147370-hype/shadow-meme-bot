"""SHADOW MEME - Solana meme coin scanner and PAPER-trading signal bot.

Analysis-only and paper-only. There is no code path that signs or sends a
transaction, and no private key or wallet is ever needed.
"""

__version__ = "0.8.0-phase8"

# Hard safety switch. This is a constant on purpose: nothing in .env or Telegram can change it.
PAPER_ONLY = True
