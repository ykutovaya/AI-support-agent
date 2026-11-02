"""SQLite: обращения, согласования и доставка отдельно от рабочего снимка retail."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from support_agent.models import AgentState
from support_agent.proposals import Proposal


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS retail_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, employee_id TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    version INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT '', UNIQUE(case_id, version));
                CREATE TABLE IF NOT EXISTS consents (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    question_id TEXT NOT NULL REFERENCES proposals(id), action_hash TEXT NOT NULL,
                    accepted INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id), digest TEXT NOT NULL,
                    employee_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(id), result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL REFERENCES proposals(id),
                    case_id TEXT NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL,
                    order_id TEXT NOT NULL, reason_code TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    UNIQUE(case_id,order_id,reason_code));
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL REFERENCES proposals(id),
                    case_id TEXT NOT NULL REFERENCES cases(id), text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL REFERENCES cases(id),
                    kind TEXT NOT NULL, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recoveries (
                    source_proposal_id TEXT PRIMARY KEY REFERENCES proposals(id),
                    case_id TEXT NOT NULL REFERENCES cases(id), body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    proposal_id TEXT REFERENCES proposals(id), reason TEXT NOT NULL DEFAULT '');
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self, snapshot, policy):
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO retail_state VALUES (1,?,?)",
                (encode(snapshot), policy),
            )

    @staticmethod
    def retail(conn):
        row = conn.execute("SELECT body,policy FROM retail_state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Рабочий магазин ещё не загружен")
        return json.loads(row["body"]), row["policy"]

    def snapshot(self):
        with self.connection() as conn:
            return self.retail(conn)

    @staticmethod
    def save_retail(conn, snapshot):
        conn.execute("UPDATE retail_state SET body=? WHERE id=1", (encode(snapshot),))

    def create_case(self, user_id, employee_id):
        case_id = uuid4().hex
        with self.transaction() as conn:
            snapshot, _ = self.retail(conn)
            if user_id not in snapshot["users"]:
                raise ValueError("Клиент отсутствует в рабочем магазине")
            conn.execute(
                "INSERT INTO cases(id,user_id,employee_id,state) VALUES (?,?,?,?)",
                (case_id, user_id, employee_id, AgentState().model_dump_json()),
            )
        return case_id

    @staticmethod
    def load_case(conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Обращение не найдено")
        result = dict(row)
        result["state"] = AgentState.model_validate_json(result["state"])
        return result

    def case(self, case_id):
        with self.connection() as conn:
            return self.load_case(conn, case_id)

    @staticmethod
    def save_case(conn, case, expected_revision):
        changed = conn.execute(
            "UPDATE cases SET state=?,revision=revision+1 WHERE id=? AND revision=?",
            (case["state"].model_dump_json(), case["id"], expected_revision),
        ).rowcount
        if changed != 1:
            raise ValueError("Обращение изменилось; повторите подготовку решения")

    @staticmethod
    def proposal(conn, proposal_id):
        row = conn.execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Предложение не найдено")
        result = dict(row)
        result["proposal"] = Proposal.model_validate_json(result.pop("body"))
        return result

    def latest(self, case_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM proposals WHERE case_id=? ORDER BY version DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            return self.proposal(conn, row["id"]) if row else None

    def ticket(self, proposal_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM support_tickets WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def next_version(conn, case_id):
        return conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]

    @staticmethod
    def add_proposal(conn, proposal):
        conn.execute(
            "UPDATE proposals SET status='stale',reason='Подготовлено новое предложение' WHERE case_id=? AND status='pending'",
            (proposal.case_id,),
        )
        conn.execute(
            "INSERT INTO proposals(id,case_id,version,body) VALUES (?,?,?,?)",
            (
                proposal.id,
                proposal.case_id,
                proposal.version,
                proposal.model_dump_json(),
            ),
        )

    @staticmethod
    def event(conn, case_id, kind, text):
        conn.execute(
            "INSERT INTO events(case_id,kind,text) VALUES (?,?,?)",
            (case_id, kind, text),
        )

    def events(self, case_id):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM events WHERE case_id=? ORDER BY id", (case_id,)
                )
            ]

    def list_cases(self, employee_id):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,user_id FROM cases WHERE employee_id=? ORDER BY rowid DESC",
                    (employee_id,),
                )
            ]
