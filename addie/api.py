"""HTTP API + static UI. Bound to 127.0.0.1 only (see __main__)."""
import json
import re
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, store
from .db import Database, WrongPasswordError
from .importers import file_importer
from .security import SessionManager

STATIC_DIR = Path(__file__).parent / "static"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------- request models (amounts from the UI are in shekels) ----------

class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = Field(pattern="^(CHECKING|CARD|SAVINGS|SECURITY|LOAN)$")
    institution: str | None = Field(default=None, max_length=100)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(expense|income|savings|transfer)$")
    color: str = Field(default="#888888", pattern="^#[0-9a-fA-F]{6}$")


class TransactionPatch(BaseModel):
    category_id: int | None = None
    note: str | None = Field(default=None, max_length=500)
    remember: bool = False  # create a rule for this merchant


class BudgetIn(BaseModel):
    monthly_amount: float = Field(ge=0, le=10_000_000)


class RuleIn(BaseModel):
    pattern: str = Field(min_length=2, max_length=100)
    category_id: int


class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_amount: float = Field(gt=0, le=1_000_000_000)
    current_amount: float = Field(default=0, ge=0, le=1_000_000_000)
    monthly_contribution: float = Field(default=0, ge=0, le=10_000_000)
    target_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ContributionIn(BaseModel):
    amount: float = Field(gt=-1_000_000_000, le=1_000_000_000)


def to_agorot(shekels: float) -> int:
    return round(shekels * 100)


def create_app(db_path=None, salt_path=None, auto_lock_minutes=None) -> FastAPI:
    db = Database(db_path or config.DB_PATH, salt_path or config.SALT_PATH)
    sessions = SessionManager(db, 60 * (auto_lock_minutes or config.AUTO_LOCK_MINUTES))

    app = FastAPI(title="Addie", docs_url=None, redoc_url=None, openapi_url=None)
    # Blocks DNS-rebinding: a malicious site cannot reach the API through another hostname.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def _idle_watcher():
        while True:
            time.sleep(30)
            sessions.expire_if_idle()

    threading.Thread(target=_idle_watcher, daemon=True).start()

    def auth(x_session: str | None = Header(default=None)):
        if not sessions.check(x_session) or not db.is_open:
            raise HTTPException(401, "locked")
        return db

    # ---------- session ----------

    @app.get("/api/status")
    def status():
        return {"initialized": db.initialized, "unlocked": db.is_open,
                "auto_lock_minutes": sessions.auto_lock_seconds // 60}

    @app.post("/api/setup")
    def setup(body: PasswordIn):
        if db.initialized:
            raise HTTPException(409, "already initialized")
        if len(body.password) < config.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"הסיסמה חייבת להכיל לפחות {config.MIN_PASSWORD_LENGTH} תווים")
        db.create(body.password)
        return {"token": sessions.start()}

    @app.post("/api/unlock")
    def unlock(body: PasswordIn):
        if not db.initialized:
            raise HTTPException(409, "not initialized")
        try:
            db.open(body.password)
        except WrongPasswordError:
            time.sleep(sessions.failed_attempt_delay())
            raise HTTPException(401, "סיסמה שגויה")
        return {"token": sessions.start()}

    @app.post("/api/lock")
    def lock():
        sessions.lock()
        return {"ok": True}

    # ---------- accounts & categories ----------

    @app.get("/api/accounts")
    def list_accounts(db=Depends(auth)):
        return db.query(
            "SELECT a.*, COALESCE(SUM(t.amount), 0) AS balance, COUNT(t.id) AS tx_count"
            " FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.id ORDER BY a.name"
        )

    @app.post("/api/accounts")
    def create_account(body: AccountIn, db=Depends(auth)):
        new_id = db.execute("INSERT INTO accounts(name, type, institution) VALUES (?, ?, ?)",
                            (body.name, body.type, body.institution))
        return {"id": new_id}

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: int, db=Depends(auth)):
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return {"ok": True}

    @app.get("/api/categories")
    def list_categories(db=Depends(auth)):
        return db.query("SELECT * FROM categories ORDER BY kind, name")

    @app.post("/api/categories")
    def create_category(body: CategoryIn, db=Depends(auth)):
        if db.one("SELECT id FROM categories WHERE name = ?", (body.name,)):
            raise HTTPException(409, "קטגוריה בשם הזה כבר קיימת")
        return {"id": db.execute("INSERT INTO categories(name, kind, color) VALUES (?, ?, ?)",
                                 (body.name, body.kind, body.color))}

    # ---------- transactions ----------

    @app.get("/api/transactions")
    def list_transactions(month: str | None = None, category_id: int | None = None,
                          uncategorized: bool = False, q: str | None = None, db=Depends(auth)):
        sql = ("SELECT t.id, t.date, t.description, t.amount, t.category_id, t.note, a.name AS account"
               " FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE 1=1")
        params: list = []
        if month:
            if not MONTH_RE.match(month):
                raise HTTPException(400, "bad month")
            start, end = store.month_bounds(month)
            sql += " AND t.date >= ? AND t.date < ?"
            params += [start, end]
        if uncategorized:
            sql += " AND t.category_id IS NULL"
        elif category_id is not None:
            sql += " AND t.category_id = ?"
            params.append(category_id)
        if q:
            sql += " AND t.description LIKE ?"
            params.append(f"%{q[:100]}%")
        sql += " ORDER BY t.date DESC, t.id DESC LIMIT 2000"
        return db.query(sql, params)

    @app.patch("/api/transactions/{tx_id}")
    def update_transaction(tx_id: int, body: TransactionPatch, db=Depends(auth)):
        tx = db.one("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        if not tx:
            raise HTTPException(404, "not found")
        fields = body.model_fields_set
        with db.transaction() as conn:
            if "category_id" in fields:
                conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (body.category_id, tx_id))
            if "note" in fields:
                conn.execute("UPDATE transactions SET note = ? WHERE id = ?", (body.note, tx_id))
            if body.remember and body.category_id is not None:
                pattern = tx["description"].strip().lower()
                conn.execute(
                    "INSERT INTO rules(pattern, category_id) VALUES (?, ?)"
                    " ON CONFLICT(pattern) DO UPDATE SET category_id = excluded.category_id",
                    (pattern, body.category_id))
                # apply to other uncategorized transactions from the same merchant
                conn.execute(
                    "UPDATE transactions SET category_id = ? WHERE category_id IS NULL AND lower(description) = ?",
                    (body.category_id, pattern))
        return {"ok": True}

    # ---------- rules ----------

    @app.get("/api/rules")
    def list_rules(db=Depends(auth)):
        return db.query("SELECT r.id, r.pattern, r.category_id, c.name AS category"
                        " FROM rules r JOIN categories c ON c.id = r.category_id ORDER BY c.name, r.pattern")

    @app.post("/api/rules")
    def create_rule(body: RuleIn, db=Depends(auth)):
        db.execute("INSERT INTO rules(pattern, category_id) VALUES (?, ?)"
                   " ON CONFLICT(pattern) DO UPDATE SET category_id = excluded.category_id",
                   (body.pattern.strip().lower(), body.category_id))
        return {"ok": True}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, db=Depends(auth)):
        db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return {"ok": True}

    # ---------- budgets & summary ----------

    @app.put("/api/budgets/{category_id}")
    def set_budget(category_id: int, body: BudgetIn, db=Depends(auth)):
        db.execute("INSERT INTO budgets(category_id, monthly_amount) VALUES (?, ?)"
                   " ON CONFLICT(category_id) DO UPDATE SET monthly_amount = excluded.monthly_amount",
                   (category_id, to_agorot(body.monthly_amount)))
        return {"ok": True}

    @app.delete("/api/budgets/{category_id}")
    def delete_budget(category_id: int, db=Depends(auth)):
        db.execute("DELETE FROM budgets WHERE category_id = ?", (category_id,))
        return {"ok": True}

    @app.get("/api/budgets")
    def list_budgets(db=Depends(auth)):
        return db.query("SELECT c.id AS category_id, c.name, c.kind, c.color, b.monthly_amount"
                        " FROM categories c LEFT JOIN budgets b ON b.category_id = c.id"
                        " WHERE c.kind IN ('expense','savings') ORDER BY c.kind, c.name")

    @app.get("/api/summary")
    def summary(month: str | None = None, db=Depends(auth)):
        month = month or date.today().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(400, "bad month")
        return store.monthly_summary(db, month)

    # ---------- savings goals ----------

    @app.get("/api/goals")
    def list_goals(db=Depends(auth)):
        return [store.goal_projection(g) for g in db.query("SELECT * FROM savings_goals ORDER BY created_at")]

    @app.post("/api/goals")
    def create_goal(body: GoalIn, db=Depends(auth)):
        return {"id": db.execute(
            "INSERT INTO savings_goals(name, target_amount, current_amount, monthly_contribution, target_date)"
            " VALUES (?, ?, ?, ?, ?)",
            (body.name, to_agorot(body.target_amount), to_agorot(body.current_amount),
             to_agorot(body.monthly_contribution), body.target_date))}

    @app.put("/api/goals/{goal_id}")
    def update_goal(goal_id: int, body: GoalIn, db=Depends(auth)):
        db.execute(
            "UPDATE savings_goals SET name = ?, target_amount = ?, current_amount = ?,"
            " monthly_contribution = ?, target_date = ? WHERE id = ?",
            (body.name, to_agorot(body.target_amount), to_agorot(body.current_amount),
             to_agorot(body.monthly_contribution), body.target_date, goal_id))
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/contribute")
    def contribute(goal_id: int, body: ContributionIn, db=Depends(auth)):
        db.execute("UPDATE savings_goals SET current_amount = MAX(current_amount + ?, 0) WHERE id = ?",
                   (to_agorot(body.amount), goal_id))
        return {"ok": True}

    @app.delete("/api/goals/{goal_id}")
    def delete_goal(goal_id: int, db=Depends(auth)):
        db.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
        return {"ok": True}

    # ---------- import ----------

    async def _read_upload(file: UploadFile) -> bytes:
        content = await file.read(config.MAX_UPLOAD_BYTES + 1)
        if len(content) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "הקובץ גדול מדי")
        return content

    @app.post("/api/import/preview")
    async def import_preview(file: UploadFile = File(...), db=Depends(auth)):
        content = await _read_upload(file)
        try:
            rows = file_importer.read_rows(file.filename or "", content)
        except Exception as exc:
            raise HTTPException(400, f"לא הצלחתי לקרוא את הקובץ: {exc}")
        mapping = file_importer.detect_mapping(rows)
        start = mapping.header_row
        return {"rows": rows[start:start + 11], "mapping": mapping.to_dict(),
                "detected": mapping.is_complete(), "all_rows_head": rows[:15]}

    @app.post("/api/import")
    async def import_file(file: UploadFile = File(...), account_id: int = Form(...),
                          mapping: str = Form(...), db=Depends(auth)):
        if not db.one("SELECT id FROM accounts WHERE id = ?", (account_id,)):
            raise HTTPException(404, "account not found")
        content = await _read_upload(file)
        try:
            m = file_importer.Mapping(**json.loads(mapping))
            rows = file_importer.read_rows(file.filename or "", content)
            txs, skipped = file_importer.parse(rows, m)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        result = store.save_transactions(db, account_id, txs)
        return {**result, "skipped_rows": skipped}

    # ---------- UI ----------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
