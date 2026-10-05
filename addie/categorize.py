"""Rule-based categorization: the longest matching pattern wins."""


def match_category(description: str, rules: list[dict]) -> int | None:
    text = description.lower()
    best = None
    for rule in rules:
        pattern = rule["pattern"]
        if pattern and pattern in text and (best is None or len(pattern) > len(best["pattern"])):
            best = rule
    return best["category_id"] if best else None


def load_rules(db) -> list[dict]:
    return db.query("SELECT pattern, category_id FROM rules")
