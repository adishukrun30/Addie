"""Business logic on top of the database: saving transactions and monthly summaries."""
import hashlib
from collections import Counter
from datetime import date

from .categorize import load_rules, match_category
from .importers import ParsedTransaction


def _dedup_hash(account_id: int, tx: ParsedTransaction, occurrence: int) -> str:
    key = f"{account_id}|{tx.date}|{tx.amount}|{tx.description.strip().lower()}|{occurrence}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def save_transactions(db, account_id: int, txs: list[ParsedTransaction], source: str = "import") -> dict:
    """Insert transactions, skipping ones already stored.

    Identical rows within one batch (two coffees, same day, same price) are kept
    apart by an occurrence counter, so re-importing the same file is a no-op.
    """
    rules = load_rules(db)
    seen = Counter()
    inserted = duplicates = 0
    with db.transaction() as conn:
        for tx in txs:
            base = (tx.date, tx.amount, tx.description.strip().lower())
            occurrence = seen[base]
            seen[base] += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO transactions(account_id, date, description, amount, category_id, source, dedup_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (account_id, tx.date, tx.description, tx.amount, match_category(tx.description, rules),
                 source, _dedup_hash(account_id, tx, occurrence)),
            )
            if cur.rowcount:
                inserted += 1
            else:
                duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates}


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(p) for p in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + (mon == 12), mon % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def previous_months(month: str, count: int) -> list[str]:
    year, mon = (int(p) for p in month.split("-"))
    out = []
    for _ in range(count):
        out.append(f"{year:04d}-{mon:02d}")
        mon -= 1
        if mon == 0:
            year, mon = year - 1, 12
    return list(reversed(out))


def _totals(db, start: str, end: str) -> dict:
    row = db.one(
        """
        SELECT
          COALESCE(SUM(CASE WHEN c.kind = 'income' OR (c.kind IS NULL AND t.amount > 0) THEN t.amount END), 0) AS income,
          COALESCE(SUM(CASE WHEN c.kind = 'expense' OR (c.kind IS NULL AND t.amount < 0) THEN -t.amount END), 0) AS expenses,
          COALESCE(SUM(CASE WHEN c.kind = 'savings' THEN -t.amount END), 0) AS savings
        FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.date >= ? AND t.date < ?
        """,
        (start, end),
    )
    return row


def monthly_summary(db, month: str) -> dict:
    start, end = month_bounds(month)
    totals = _totals(db, start, end)
    income, expenses, savings = totals["income"], totals["expenses"], totals["savings"]

    by_category = db.query(
        """
        SELECT c.id, c.name, c.kind, c.color,
               COALESCE(-SUM(t.amount), 0) AS spent,
               b.monthly_amount AS budget
        FROM categories c
        LEFT JOIN transactions t ON t.category_id = c.id AND t.date >= ? AND t.date < ?
        LEFT JOIN budgets b ON b.category_id = c.id
        WHERE c.kind IN ('expense', 'savings')
        GROUP BY c.id
        HAVING spent != 0 OR budget IS NOT NULL
        ORDER BY spent DESC
        """,
        (start, end),
    )
    uncategorized = db.one(
        "SELECT COUNT(*) AS n FROM transactions WHERE category_id IS NULL AND date >= ? AND date < ?",
        (start, end),
    )["n"]

    trend = []
    for m in previous_months(month, 6):
        s, e = month_bounds(m)
        t = _totals(db, s, e)
        trend.append({"month": m, **t})

    left_over = income - expenses - savings
    return {
        "month": month,
        "income": income,
        "expenses": expenses,
        "savings": savings,
        "left_over": left_over,
        # share of income that was not spent (explicit savings + what is left over)
        "savings_rate": round((income - expenses) / income, 4) if income > 0 else None,
        "categories": by_category,
        "uncategorized": uncategorized,
        "trend": trend,
    }


def goal_projection(goal: dict) -> dict:
    remaining = max(goal["target_amount"] - goal["current_amount"], 0)
    months_left = None
    if remaining == 0:
        months_left = 0
    elif goal["monthly_contribution"] > 0:
        months_left = -(-remaining // goal["monthly_contribution"])  # ceil
    return {
        **goal,
        "remaining": remaining,
        "progress": round(min(goal["current_amount"] / goal["target_amount"], 1), 4),
        "months_left": months_left,
    }
