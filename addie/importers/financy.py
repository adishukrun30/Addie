"""Automatic bank sync through Financy (Israeli open-banking aggregator).

Not implemented yet: Financy's API is only available on its Starter and Pro
plans. Once the account has API access, implement `fetch_transactions` to
return `ParsedTransaction` objects and pass them to
`store.save_transactions(db, account_id, txs, source="financy")`; categorization
and de-duplication are already handled there.
"""
from . import ParsedTransaction


class FinancyNotConfigured(RuntimeError):
    pass


def fetch_transactions(since: str) -> list[ParsedTransaction]:
    raise FinancyNotConfigured("החיבור ל-Financy דורש מסלול Starter או Pro עם גישת API")
