"""Transaction sources.

Every source produces `ParsedTransaction` objects; `store.save_transactions`
handles categorization and de-duplication, so adding a source (Financy,
another bank API) only means writing a new parser/fetcher.
"""
from dataclasses import dataclass


@dataclass
class ParsedTransaction:
    date: str          # YYYY-MM-DD
    description: str
    amount: int        # agorot; negative = money out
