import streamlit as st
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import List, Tuple, Dict, Optional
import itertools
import smtplib
from email.mime.text import MIMEText
import urllib.request
import urllib.error
import json
import jpholiday

DB_PATH = Path("summer_vacation.db")

ROLE_CHIEF = "chief"
ROLE_CLINICAL = "clinical"
ROLE_PATHOLOGY = "pathology"

USER_STAFF = "staff"
USER_RESIDENT = "resident"

STATUS_SUBMITTED = "submitted"
STATUS_TENTATIVE = "tentative"
STATUS_CONFIRMED = "confirmed"
STATUS_CONFLICT = "conflict"
STATUS_REJECTED = "rejected"

ABSENCE_VACATION = "summer_vacation"

DEFAULT_SETTINGS = {
    "season_start": "2026-06-01",
    "season_end": "2027-02-28",
    "initial_deadline": "2026-06-15 23:59",
    "required_workdays": "10",
    "staff_max_off": "1",
    "min_chief": "1",
    "min_chief_clinical": "5",
    "min_total_residents": "7",
    "allow_staff_exception": "false",
    "slack_webhook_url": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "mail_from": ""
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('staff', 'resident')),
            email TEXT,
            slack_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS resident_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('chief', 'clinical', 'pathology')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS absences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            absence_type TEXT NOT NULL,
            description TEXT,
            counts_as_unavailable INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS request_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            preference_rank INTEGER NOT NULL CHECK(preference_rank BETWEEN 1 AND 5),
            status TEXT NOT NULL DEFAULT 'submitted',
            submitted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS request_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id INTEGER NOT NULL,
            vacation_date TEXT NOT NULL,
            FOREIGN KEY(pattern_id) REFERENCES request_patterns(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            vacation_date TEXT NOT NULL,
            source_pattern_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('tentative', 'confirmed')),
            confirmed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(source_pattern_id) REFERENCES request_patterns(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            conflict_date TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            involved_user_ids TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS non_working_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holiday_date TEXT NOT NULL UNIQUE,
            label TEXT
        )
    """)

    for k, v in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    # 日本の祝日を自動登録
    try:
        start_year = int(DEFAULT_SETTINGS["season_start"][:4])
        end_year = int(DEFAULT_SETTINGS["season_end"][:4])
        for year in range(start_year, end_year + 1):
            for holiday_date, holiday_name in jpholiday.year_holidays(year):
                cur.execute(
                    "INSERT OR IGNORE INTO non_working_days(holiday_date, label) VALUES (?, ?)",
                    (holiday_date.isoformat(), holiday_name)
                )
    except Exception:
        pass

    conn.commit()
    conn.close()


def get_setting(key: str) -> str:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def to_dates(start: date, end: date) -> List[date]:
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def get_non_working_days() -> set:
    conn = connect()
    rows = conn.execute("SELECT holiday_date FROM non_working_days").fetchall()
    conn.close()
    return {parse_date(r["holiday_date"]) for r in rows}


def is_workday(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in get_non_working_days()


def workdays_between(start: date, end: date) -> List[date]:
    return [d for d in to_dates(start, end) if is_workday(d)]


def rows_to_dicts(rows) -> List[Dict]:
    return [dict(r) for r in rows]


def get_users(active_only: bool = True) -> List[Dict]:
    conn = connect()
    q = "SELECT * FROM users"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY category, name"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_user(user_id: int) -> Optional[Dict]:
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_user(name: str, category: str, email: str, slack_id: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO users(name, category, email, slack_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, category, email, slack_id, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def update_user(user_id: int, name: str, category: str, email: str, slack_id: str, active: bool) -> None:
    conn = connect()
    conn.execute(
        "UPDATE users SET name=?, category=?, email=?, slack_id=?, active=? WHERE id=?",
        (name, category, email, slack_id, 1 if active else 0, user_id),
    )
    conn.commit()
    conn.close()


def add_role(user_id: int, start: date, end: date, role: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO resident_roles(user_id, start_date, end_date, role) VALUES (?, ?, ?, ?)",
        (user_id, start.isoformat(), end.isoformat(), role),
    )
    conn.commit()
    conn.close()


def get_roles(user_id: Optional[int] = None) -> List[Dict]:
    conn = connect()
    if user_id is None:
        rows = conn.execute("""
            SELECT rr.*, u.name FROM resident_roles rr
            JOIN users u ON rr.user_id = u.id
            ORDER BY u.name, rr.start_date
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT rr.*, u.name FROM resident_roles rr
            JOIN users u ON rr.user_id = u.id
            WHERE rr.user_id = ?
            ORDER BY rr.start_date
        """, (user_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def delete_role(role_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM resident_roles WHERE id = ?", (role_id,))
    conn.commit()
    conn.close()


def role_on_date(user_id: int, d: date) -> Optional[str]:
    conn = connect()
    row = conn.execute("""
        SELECT role FROM resident_roles
        WHERE user_id = ? AND start_date <= ? AND end_date >= ?
        ORDER BY start_date DESC
        LIMIT 1
    """, (user_id, d.isoformat(), d.isoformat())).fetchone()
    conn.close()
    return row["role"] if row else None


def add_absence(user_id: int, start: date, end: date, absence_type: str, description: str, counts: bool = True) -> None:
    conn = connect()
    conn.execute("""
        INSERT INTO absences(user_id, start_date, end_date, absence_type, description, counts_as_unavailable, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        start.isoformat(),
        end.isoformat(),
        absence_type,
        description,
        1 if counts else 0,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()


def get_absences(user_id: Optional[int] = None) -> List[Dict]:
    conn = connect()
    if user_id is None:
        rows = conn.execute("""
            SELECT a.*, u.name FROM absences a
            JOIN users u ON a.user_id = u.id
            ORDER BY a.start_date, u.name
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.*, u.name FROM absences a
            JOIN users u ON a.user_id = u.id
            WHERE a.user_id = ?
            ORDER BY a.start_date
        """, (user_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def delete_absence(absence_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM absences WHERE id = ?", (absence_id,))
    conn.commit()
    conn.close()


def add_or_replace_request(user_id: int, rank: int, vacation_dates: List[date], note: str = "") -> Tuple[bool, str]:
    required = int(get_setting("required_workdays"))
    workdays = sorted({d for d in vacation_dates if is_workday(d)})

    season_start = parse_date(get_setting("season_start"))
    season_end = parse_date(get_setting("season_end"))
    outside = [d for d in workdays if d < season_start or d > season_end]
    if outside:
        return False, f"対象期間外の日付が含まれています: {', '.join(d.isoformat() for d in outside)}"

    if len(workdays) < 1:
        return False, "少なくとも1勤務日以上を選択してください。"

    conn_check = connect()
    existing_rows = conn_check.execute("""
        SELECT rd.vacation_date
        FROM request_patterns rp
        JOIN request_dates rd ON rp.id = rd.pattern_id
        WHERE rp.user_id = ?
    """, (user_id,)).fetchall()
    conn_check.close()

    existing_dates = {parse_date(row["vacation_date"]) for row in existing_rows}
    total_dates = existing_dates | set(workdays)

    if len(total_dates) > required:
        return False, (
            f"このユーザーの希望休暇日が合計{len(total_dates)}勤務日になります。"
            f"上限は{required}勤務日です。既存の希望を削除または短縮してください。"
        )

    conn = connect()
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute("""
        INSERT INTO request_patterns(user_id, preference_rank, status, submitted_at, updated_at, note)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, rank, STATUS_SUBMITTED, now, now, note))
    pattern_id = cur.lastrowid

    for d in workdays:
        conn.execute(
            "INSERT INTO request_dates(pattern_id, vacation_date) VALUES (?, ?)",
            (pattern_id, d.isoformat())
        )
    conn.commit()
    conn.close()
    return True, f"保存しました。このユーザーの希望休暇日は合計{len(total_dates)}勤務日です。"



def get_requests(user_id: Optional[int] = None) -> List[Dict]:
    conn = connect()
    if user_id is None:
        rows = conn.execute("""
            SELECT rp.*, u.name, u.category
            FROM request_patterns rp
            JOIN users u ON rp.user_id = u.id
            ORDER BY u.name, rp.preference_rank
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT rp.*, u.name, u.category
            FROM request_patterns rp
            JOIN users u ON rp.user_id = u.id
            WHERE rp.user_id = ?
            ORDER BY rp.preference_rank
        """, (user_id,)).fetchall()
    result = []
    for r in rows:
        drows = conn.execute(
            "SELECT vacation_date FROM request_dates WHERE pattern_id = ? ORDER BY vacation_date",
            (r["id"],)
        ).fetchall()
        item = dict(r)
        item["dates"] = [x["vacation_date"] for x in drows]
        result.append(item)
    conn.close()
    return result


def delete_request(pattern_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM request_dates WHERE pattern_id = ?", (pattern_id,))
    conn.execute("DELETE FROM request_patterns WHERE id = ?", (pattern_id,))
    conn.commit()
    conn.close()


def current_unavailable_dates() -> Dict[Tuple[int, date], str]:
    unavailable = {}
    for a in get_absences():
        if int(a["counts_as_unavailable"]) != 1:
            continue
        for d in workdays_between(parse_date(a["start_date"]), parse_date(a["end_date"])):
            unavailable[(a["user_id"], d)] = a["absence_type"]
    for assn in get_assignments(statuses=[STATUS_TENTATIVE, STATUS_CONFIRMED]):
        unavailable[(assn["user_id"], parse_date(assn["vacation_date"]))] = ABSENCE_VACATION
    return unavailable


def get_assignments(statuses: Optional[List[str]] = None) -> List[Dict]:
    conn = connect()
    if statuses:
        qmarks = ",".join("?" for _ in statuses)
        rows = conn.execute(f"""
            SELECT a.*, u.name, u.category
            FROM assignments a
            JOIN users u ON a.user_id = u.id
            WHERE a.status IN ({qmarks})
            ORDER BY a.vacation_date, u.name
        """, statuses).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.*, u.name, u.category
            FROM assignments a
            JOIN users u ON a.user_id = u.id
            ORDER BY a.vacation_date, u.name
        """).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def clear_tentative_assignments() -> None:
    conn = connect()
    conn.execute("DELETE FROM assignments WHERE status = ?", (STATUS_TENTATIVE,))
    conn.commit()
    conn.close()


def add_assignment(user_id: int, d: date, pattern_id: Optional[int], status: str) -> None:
    conn = connect()
    existing = conn.execute("""
        SELECT id FROM assignments
        WHERE user_id = ? AND vacation_date = ? AND status IN ('tentative', 'confirmed')
    """, (user_id, d.isoformat())).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO assignments(user_id, vacation_date, source_pattern_id, status, confirmed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            d.isoformat(),
            pattern_id,
            status,
            datetime.now().isoformat(timespec="seconds") if status == STATUS_CONFIRMED else None
        ))
    conn.commit()
    conn.close()


def confirm_tentative_assignments() -> None:
    conn = connect()
    conn.execute("""
        UPDATE assignments
        SET status = 'confirmed', confirmed_at = ?
        WHERE status = 'tentative'
    """, (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()


def delete_assignment(assignment_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()


def update_request_status(pattern_id: int, status: str) -> None:
    conn = connect()
    conn.execute("UPDATE request_patterns SET status=?, updated_at=? WHERE id=?",
                 (status, datetime.now().isoformat(timespec="seconds"), pattern_id))
    conn.commit()
    conn.close()


def reset_conflicts() -> str:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    conn = connect()
    conn.execute("UPDATE conflicts SET resolved = 1 WHERE resolved = 0")
    conn.commit()
    conn.close()
    return run_id


def add_conflict(run_id: str, d: date, conflict_type: str, detail: str, involved_user_ids: List[int]) -> None:
    conn = connect()
    conn.execute("""
        INSERT INTO conflicts(run_id, conflict_date, conflict_type, detail, involved_user_ids, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        d.isoformat(),
        conflict_type,
        detail,
        json.dumps(sorted(set(involved_user_ids))),
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()


def get_conflicts(open_only: bool = True) -> List[Dict]:
    conn = connect()
    q = "SELECT * FROM conflicts"
    if open_only:
        q += " WHERE resolved = 0"
    q += " ORDER BY conflict_date, conflict_type"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def set_pattern_statuses(pattern_ids: List[int], status: str) -> None:
    conn = connect()
    for pid in pattern_ids:
        conn.execute("UPDATE request_patterns SET status=?, updated_at=? WHERE id=?",
                     (status, datetime.now().isoformat(timespec="seconds"), pid))
    conn.commit()
    conn.close()


def evaluate_dates(assignments: List[Tuple[int, date]], run_id: Optional[str] = None) -> Tuple[bool, List[Dict]]:
    users = get_users()
    user_map = {u["id"]: u for u in users}
    season_start = parse_date(get_setting("season_start"))
    season_end = parse_date(get_setting("season_end"))
    staff_max_off = int(get_setting("staff_max_off"))
    min_chief = int(get_setting("min_chief"))
    min_chief_clinical = int(get_setting("min_chief_clinical"))
    min_total_residents = int(get_setting("min_total_residents"))

    unavailable = current_unavailable_dates()
    for user_id, d in assignments:
        unavailable[(user_id, d)] = ABSENCE_VACATION

    relevant_dates = sorted({d for _, d in assignments} | {d for _, d in unavailable.keys()})
    relevant_dates = [d for d in relevant_dates if season_start <= d <= season_end and is_workday(d)]

    conflicts = []
    for d in relevant_dates:
        off_user_ids = [uid for (uid, dd), reason in unavailable.items() if dd == d]
        staff_off = [uid for uid in off_user_ids if user_map.get(uid, {}).get("category") == USER_STAFF]
        if len(staff_off) > staff_max_off:
            conflicts.append({
                "date": d,
                "type": "staff_overlap",
                "detail": f"スタッフ不在が{len(staff_off)}名です。上限は{staff_max_off}名です。",
                "involved": staff_off
            })

        resident_users = [u for u in users if u["category"] == USER_RESIDENT and u["active"] == 1]
        present_roles = []
        absent_residents = []
        for u in resident_users:
            uid = u["id"]
            role = role_on_date(uid, d)
            if role is None:
                continue
            if uid in off_user_ids:
                absent_residents.append(uid)
            else:
                present_roles.append((uid, role))

        chief_present = [uid for uid, role in present_roles if role == ROLE_CHIEF]
        clinical_present = [uid for uid, role in present_roles if role in (ROLE_CHIEF, ROLE_CLINICAL)]
        total_present = [uid for uid, role in present_roles if role in (ROLE_CHIEF, ROLE_CLINICAL, ROLE_PATHOLOGY)]

        if len(chief_present) < min_chief:
            conflicts.append({
                "date": d,
                "type": "no_chief",
                "detail": f"チーフ在席が{len(chief_present)}名です。最低{min_chief}名が必要です。",
                "involved": absent_residents
            })
        if len(clinical_present) < min_chief_clinical:
            conflicts.append({
                "date": d,
                "type": "insufficient_clinical",
                "detail": f"チーフ＋臨床レジデント在席が{len(clinical_present)}名です。最低{min_chief_clinical}名が必要です。",
                "involved": absent_residents
            })
        if len(total_present) < min_total_residents:
            conflicts.append({
                "date": d,
                "type": "insufficient_total_residents",
                "detail": f"チーフ＋臨床＋病理レジデント在席が{len(total_present)}名です。最低{min_total_residents}名が必要です。",
                "involved": absent_residents
            })

    if run_id:
        for c in conflicts:
            add_conflict(run_id, c["date"], c["type"], c["detail"], c["involved"])

    return len(conflicts) == 0, conflicts


def find_batch_solution() -> Tuple[bool, List[Dict], List[Tuple[int, int, List[date]]]]:
    """
    Returns:
        ok, conflicts, selected
        selected = [(user_id, pattern_id, dates)]
    This function does not prioritize first-submitted requests.
    It chooses the combination with the lowest total preference-rank sum.
    If no valid combination exists, it returns conflicts for the best-ranked attempted combination.
    """
    users = get_users()
    active_users = [u for u in users if u["active"] == 1]
    user_requests = {}
    for u in active_users:
        reqs = get_requests(u["id"])
        if reqs:
            user_requests[u["id"]] = reqs

    if not user_requests:
        return False, [], []

    candidate_lists = []
    for uid, reqs in user_requests.items():
        opts = []
        for r in reqs:
            dates = [parse_date(x) for x in r["dates"]]
            opts.append((uid, r["id"], int(r["preference_rank"]), dates))
        opts.sort(key=lambda x: x[2])
        candidate_lists.append(opts)

    best_attempt = None
    best_score = 10**9
    best_conflicts = []
    max_combinations = 200000
    checked = 0

    for combo in itertools.product(*candidate_lists):
        checked += 1
        if checked > max_combinations:
            break

        selected_assignments = []
        pattern_ids = []
        score = 0
        for uid, pid, rank, dates in combo:
            score += rank
            pattern_ids.append(pid)
            for d in dates:
                selected_assignments.append((uid, d))

        ok, conflicts = evaluate_dates(selected_assignments)
        if ok:
            return True, [], [(uid, pid, dates) for uid, pid, rank, dates in combo]

        if score < best_score:
            best_score = score
            best_attempt = combo
            best_conflicts = conflicts

    selected = []
    if best_attempt:
        selected = [(uid, pid, dates) for uid, pid, rank, dates in best_attempt]
    return False, best_conflicts, selected


def send_slack(message: str) -> Tuple[bool, str]:
    url = get_setting("slack_webhook_url").strip()
    if not url:
        return False, "Slack webhook URLが未設定です。"
    data = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            if 200 <= res.status < 300:
                return True, "Slack通知を送信しました。"
            return False, f"Slack通知エラー: HTTP {res.status}"
    except urllib.error.URLError as e:
        return False, f"Slack通知エラー: {e}"


def send_email(to_addr: str, subject: str, body: str) -> Tuple[bool, str]:
    host = get_setting("smtp_host").strip()
    user = get_setting("smtp_user").strip()
    password = get_setting("smtp_password")
    mail_from = get_setting("mail_from").strip() or user
    port = int(get_setting("smtp_port") or "587")

    if not host or not mail_from or not to_addr:
        return False, "SMTP設定または宛先が不足しています。"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            if user:
                server.login(user, password)
            server.sendmail(mail_from, [to_addr], msg.as_string())
        return True, "メールを送信しました。"
    except Exception as e:
        return False, f"メール送信エラー: {e}"


def conflict_user_names(conflict: Dict) -> str:
    users = {u["id"]: u["name"] for u in get_users(active_only=False)}
    try:
        ids = json.loads(conflict["involved_user_ids"])
    except Exception:
        ids = []
    return ", ".join(users.get(i, f"ID:{i}") for i in ids)


def build_conflict_message(conflicts: List[Dict]) -> str:
    if not conflicts:
        return "コンフリクトはありません。"
    lines = ["夏季休暇調整でコンフリクトが検出されました。"]
    for c in conflicts:
        if "conflict_date" in c:
            d = c["conflict_date"]
            typ = c["conflict_type"]
            detail = c["detail"]
            users = conflict_user_names(c)
        else:
            d = c["date"].isoformat()
            typ = c["type"]
            detail = c["detail"]
            user_map = {u["id"]: u["name"] for u in get_users(active_only=False)}
            users = ", ".join(user_map.get(i, f"ID:{i}") for i in c["involved"])
        lines.append(f"- {d}: {typ} / {detail} / 対象: {users}")
    return "\n".join(lines)


def render_user_selector(label: str = "ユーザー") -> Optional[int]:
    users = get_users()
    if not users:
        st.warning("ユーザーが登録されていません。")
        return None

    options = {"選択してください": None}
    options.update({f'{u["name"]} ({u["category"]})': u["id"] for u in users})

    selected = st.selectbox(label, list(options.keys()), index=0)

    if options[selected] is None:
        st.info("まずユーザーを選択してください。")
        return None

    return options[selected]



def calendar_date_picker(label: str, key: str, default: date) -> date:
    st.markdown(f"**{label}**")

    if f"{key}_selected" not in st.session_state:
        st.session_state[f"{key}_selected"] = default

    selected = st.session_state[f"{key}_selected"]

    c1, c2 = st.columns(2)
    with c1:
        year = st.selectbox(
            "年",
            list(range(2026, 2028)),
            index=list(range(2026, 2028)).index(selected.year) if selected.year in range(2026, 2028) else 0,
            key=f"{key}_year",
        )
    with c2:
        month = st.selectbox(
            "月",
            list(range(1, 13)),
            index=selected.month - 1,
            key=f"{key}_month",
        )

    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    non_working = get_non_working_days()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    cols = st.columns(7)
    for i, w in enumerate(weekdays):
        cols[i].markdown(f"<div style='text-align:center;font-weight:bold'>{w}</div>", unsafe_allow_html=True)

    days = []
    for _ in range(first.weekday()):
        days.append(None)
    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    while len(days) % 7 != 0:
        days.append(None)

    for week_start in range(0, len(days), 7):
        cols = st.columns(7)
        for i, d in enumerate(days[week_start:week_start + 7]):
            if d is None:
                cols[i].markdown(" ")
                continue

            is_holiday = d in non_working
            is_weekend = d.weekday() >= 5
            is_selected = d == selected

            if is_selected:
                label_text = f"✅ {d.day}"
            elif is_holiday:
                label_text = f"祝 {d.day}"
            elif is_weekend:
                label_text = f"休 {d.day}"
            else:
                label_text = str(d.day)

            if cols[i].button(label_text, key=f"{key}_{d.isoformat()}"):
                st.session_state[f"{key}_selected"] = d
                st.rerun()

    selected = st.session_state[f"{key}_selected"]

    if selected.weekday() >= 5:
        st.warning(f"{selected.isoformat()} は土日です。休暇勤務日数には含まれません。")
    elif selected in non_working:
        st.warning(f"{selected.isoformat()} は登録済み非勤務日です。休暇勤務日数には含まれません。")
    else:
        st.success(f"選択日: {selected.isoformat()}")

    return selected




def render_month_preview(year: int, month: int, selected_dates: List[date]) -> None:
    selected_set = set(selected_dates)
    non_working = get_non_working_days()

    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    days = []
    for _ in range(first.weekday()):
        days.append(None)

    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)

    while len(days) % 7 != 0:
        days.append(None)

    html = """
    <style>
    .cal-table {border-collapse: collapse; width: 100%; table-layout: fixed; margin-top: 8px;}
    .cal-table th {text-align: center; padding: 6px; font-size: 13px; color: #555;}
    .cal-table td {height: 42px; text-align: center; vertical-align: middle; border: 1px solid #eee; border-radius: 6px;}
    .cal-workday {background: #ffffff;}
    .cal-sat {background: #e8f1ff; color: #2457a6;}
    .cal-sun {background: #ffecec; color: #b42318;}
    .cal-holiday {background: #ffe2e2; color: #b42318; font-weight: 700;}
    .cal-selected {background: #d9fbe5 !important; color: #046c4e !important; font-weight: 800; border: 2px solid #22c55e !important;}
    .cal-empty {background: #fafafa;}
    .cal-small {font-size: 10px; display:block; opacity:0.8;}
    </style>
    <table class="cal-table">
    <tr><th>月</th><th>火</th><th>水</th><th>木</th><th>金</th><th>土</th><th>日</th></tr>
    """

    for i in range(0, len(days), 7):
        html += "<tr>"
        for d in days[i:i+7]:
            if d is None:
                html += '<td class="cal-empty"></td>'
                continue

            cls = "cal-workday"
            label = str(d.day)
            sub = ""

            if d.weekday() == 5:
                cls = "cal-sat"
                sub = "土"
            if d.weekday() == 6:
                cls = "cal-sun"
                sub = "日"
            if d in non_working:
                cls = "cal-holiday"
                sub = "祝"
            if d in selected_set:
                cls += " cal-selected"
                sub = "選択"

            html += f'<td class="{cls}">{label}<span class="cal-small">{sub}</span></td>'
        html += "</tr>"

    html += "</table>"
    st.markdown(html, unsafe_allow_html=True)



def select_date_with_colored_calendar(label: str, key: str, default: date) -> date:
    st.markdown(f"**{label}**")

    if f"{key}_selected" not in st.session_state:
        st.session_state[f"{key}_selected"] = default

    selected = st.session_state[f"{key}_selected"]

    c1, c2 = st.columns(2)
    with c1:
        year = st.selectbox(
            "年",
            list(range(2026, 2028)),
            index=list(range(2026, 2028)).index(selected.year) if selected.year in range(2026, 2028) else 0,
            key=f"{key}_year",
        )
    with c2:
        month = st.selectbox(
            "月",
            list(range(1, 13)),
            index=selected.month - 1,
            key=f"{key}_month",
        )

    first = date(year, month, 1)
    last = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)

    non_working = get_non_working_days()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    cols = st.columns(7)
    for i, w in enumerate(weekdays):
        cols[i].markdown(f"<div style='text-align:center;font-weight:bold'>{w}</div>", unsafe_allow_html=True)

    days = [None] * first.weekday()
    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    while len(days) % 7 != 0:
        days.append(None)

    for week_start in range(0, len(days), 7):
        cols = st.columns(7)
        for i, d in enumerate(days[week_start:week_start + 7]):
            if d is None:
                cols[i].markdown(" ")
                continue

            if d == selected:
                text = f"✅ {d.day}"
            elif d in non_working:
                text = f"祝 {d.day}"
            elif d.weekday() == 5:
                text = f"土 {d.day}"
            elif d.weekday() == 6:
                text = f"日 {d.day}"
            else:
                text = str(d.day)

            if cols[i].button(text, key=f"{key}_{d.isoformat()}"):
                st.session_state[f"{key}_selected"] = d
                st.rerun()

    return st.session_state[f"{key}_selected"]



def compact_date_selector(label: str, key: str, default: date) -> date:
    st.markdown(f"**{label}**")

    season_start = parse_date(get_setting("season_start"))
    season_end = parse_date(get_setting("season_end"))

    years = list(range(season_start.year, season_end.year + 1))

    if f"{key}_year" not in st.session_state:
        st.session_state[f"{key}_year"] = default.year
    if f"{key}_month" not in st.session_state:
        st.session_state[f"{key}_month"] = default.month
    if f"{key}_day" not in st.session_state:
        st.session_state[f"{key}_day"] = default.day

    c1, c2, c3 = st.columns(3)

    with c1:
        year = st.selectbox(
            "年",
            years,
            index=years.index(st.session_state[f"{key}_year"]) if st.session_state[f"{key}_year"] in years else 0,
            key=f"{key}_year_select",
        )

    with c2:
        month = st.selectbox(
            "月",
            list(range(1, 13)),
            index=st.session_state[f"{key}_month"] - 1,
            key=f"{key}_month_select",
        )

    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day

    day_default = min(st.session_state[f"{key}_day"], last_day)

    with c3:
        day = st.selectbox(
            "日",
            list(range(1, last_day + 1)),
            index=day_default - 1,
            key=f"{key}_day_select",
        )

    selected = date(year, month, day)

    st.session_state[f"{key}_year"] = year
    st.session_state[f"{key}_month"] = month
    st.session_state[f"{key}_day"] = day

    non_working = get_non_working_days()
    if selected.weekday() >= 5:
        st.warning(f"{selected.isoformat()} は土日です。勤務日数には含まれません。")
    elif selected in non_working:
        st.warning(f"{selected.isoformat()} は登録済み非勤務日です。勤務日数には含まれません。")
    else:
        st.success(f"{selected.isoformat()} は勤務日です。")

    return selected



def mobile_range_calendar(label: str, key: str, default_start: Optional[date], default_end: Optional[date]) -> Tuple[Optional[date], Optional[date]]:
    st.markdown(f"**{label}**")

    season_start = parse_date(get_setting("season_start"))
    season_end = parse_date(get_setting("season_end"))
    non_working = get_non_working_days()

    start_key = f"{key}_start"
    end_key = f"{key}_end"
    ym_key = f"{key}_ym"

    if start_key not in st.session_state:
        st.session_state[start_key] = default_start
    if end_key not in st.session_state:
        st.session_state[end_key] = default_end
    if ym_key not in st.session_state:
        # 初期表示月は対象期間の開始月。日付は未選択。
        st.session_state[ym_key] = date(season_start.year, season_start.month, 1)

    month_base = st.session_state[ym_key]

    c_prev, c_title, c_next = st.columns([1, 3, 1])
    with c_prev:
        if st.button("‹", key=f"{key}_prev"):
            y = month_base.year
            m = month_base.month - 1
            if m == 0:
                y -= 1
                m = 12
            new_month = date(y, m, 1)
            if new_month >= date(season_start.year, season_start.month, 1):
                st.session_state[ym_key] = new_month
            st.rerun()

    with c_title:
        st.markdown(
            f"<div style='text-align:center;font-weight:700;font-size:1.05rem'>{month_base.year}年 {month_base.month}月</div>",
            unsafe_allow_html=True
        )

    with c_next:
        if st.button("›", key=f"{key}_next"):
            y = month_base.year
            m = month_base.month + 1
            if m == 13:
                y += 1
                m = 1
            new_month = date(y, m, 1)
            if new_month <= date(season_end.year, season_end.month, 1):
                st.session_state[ym_key] = new_month
            st.rerun()

    st.caption("1回目で開始日、2回目で終了日を選択。🔵土曜、🔴日曜・祝日、🟩選択範囲。")

    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    header_cols = st.columns(7)
    for i, w in enumerate(weekdays):
        header_cols[i].markdown(
            f"<div style='text-align:center;font-weight:700;font-size:0.85rem'>{w}</div>",
            unsafe_allow_html=True
        )

    first = month_base
    if first.month == 12:
        last = date(first.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(first.year, first.month + 1, 1) - timedelta(days=1)

    days = [None] * first.weekday()
    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    while len(days) % 7 != 0:
        days.append(None)

    current_start = st.session_state[start_key]
    current_end = st.session_state[end_key]

    if current_start is not None and current_end is not None and current_end < current_start:
        current_end = current_start
        st.session_state[end_key] = current_start

    for week_start in range(0, len(days), 7):
        cols = st.columns(7)
        for i, d in enumerate(days[week_start:week_start + 7]):
            if d is None:
                cols[i].markdown(" ")
                continue

            outside_season = d < season_start or d > season_end
            is_holiday = d in non_working
            is_saturday = d.weekday() == 5
            is_sunday = d.weekday() == 6

            in_range = False
            if current_start is not None and current_end is None:
                in_range = d == current_start
            elif current_start is not None and current_end is not None:
                in_range = current_start <= d <= current_end

            if outside_season:
                label_text = f"· {d.day}"
            elif in_range:
                label_text = f"🟩 {d.day}"
            elif is_holiday:
                label_text = f"🔴祝 {d.day}"
            elif is_sunday:
                label_text = f"🔴 {d.day}"
            elif is_saturday:
                label_text = f"🔵 {d.day}"
            else:
                label_text = f"　{d.day}"

            if cols[i].button(label_text, key=f"{key}_{d.isoformat()}", disabled=outside_season, use_container_width=True):
                s0 = st.session_state[start_key]
                e0 = st.session_state[end_key]

                if s0 is None:
                    # 1回目：開始日を選択
                    st.session_state[start_key] = d
                    st.session_state[end_key] = None
                elif e0 is None:
                    # 2回目：終了日を選択。開始日より前なら開始日を選び直し。
                    if d >= s0:
                        st.session_state[end_key] = d
                    else:
                        st.session_state[start_key] = d
                        st.session_state[end_key] = None
                else:
                    # 既に範囲選択済みなら、新しい開始日として選び直し。
                    st.session_state[start_key] = d
                    st.session_state[end_key] = None

                st.rerun()

    ns = st.session_state[start_key]
    ne = st.session_state[end_key]

    if ns is None:
        st.info("開始日を選択してください。")
        return None, None

    if ne is None:
        st.info(f"開始日: {ns.isoformat()}。次に終了日を選択してください。")
        return ns, None

    if ne < ns:
        ne = ns
        st.session_state[end_key] = ns

    workdays = workdays_between(ns, ne)

    st.markdown(
        f"**選択範囲:** {ns.isoformat()} 〜 {ne.isoformat()} / "
        f"**{len(workdays)}勤務日**（土日・登録済み非勤務日は除外）"
    )

    if ns.weekday() >= 5 or ns in non_working:
        st.warning("開始日は非勤務日です。勤務日数には含まれません。")
    if ne.weekday() >= 5 or ne in non_working:
        st.warning("終了日は非勤務日です。勤務日数には含まれません。")

    return ns, ne




def page_absences():
    st.header("夏季休暇以外の不在入力")
    uid = render_user_selector()
    if uid is None:
        return

    st.subheader("不在を追加")

    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input("不在開始日", date.today(), key="absence_start")
    with c2:
        end = st.date_input("不在終了日", date.today(), key="absence_end")

    absence_type = st.selectbox(
        "不在種別",
        ["年休", "出張", "学会", "外勤", "研究日", "病休", "その他"]
    )
    counts = st.checkbox("臨床不在としてカウントする", value=True)
    desc = st.text_input("備考")

    if st.button("不在を追加", use_container_width=True):
        if end < start:
            st.error("終了日は開始日以降にしてください。")
        else:
            add_absence(uid, start, end, absence_type, desc, counts)
            st.success("追加しました。")
            st.rerun()

    st.markdown("---")
    st.subheader("自分の不在一覧")

    rows = get_absences(uid)
    if not rows:
        st.info("登録済みの不在はありません。")
        return

    for r in rows:
        with st.container(border=True):
            st.markdown(
                f"**{r['absence_type']}**　"
                f"{r['start_date']} 〜 {r['end_date']}"
            )
            if r.get("description"):
                st.caption(f"備考: {r['description']}")
            st.caption(
                "臨床不在としてカウント: "
                + ("はい" if int(r["counts_as_unavailable"]) == 1 else "いいえ")
            )

            if st.button("この不在を削除", key=f"delete_absence_{r['id']}", use_container_width=True):
                delete_absence(int(r["id"]))
                st.success("削除しました。")
                st.rerun()


def page_requests():
    st.header("夏季休暇希望入力")
    uid = render_user_selector()
    if uid is None:
        return

    required = int(get_setting("required_workdays"))
    st.info(
        f"勤務日を選択してください。複数回に分けて入力できます。"
        f"合計{required}勤務日まで保存できます。土日と登録済み非勤務日は自動的に除外されます。"
    )

    rank = 1

    st.subheader("休暇ブロックを追加")

    if "date_blocks" not in st.session_state:
        st.session_state.date_blocks = [(None, None)]

    new_blocks = []
    selected = []

    for i, (s0, e0) in enumerate(st.session_state.date_blocks):
        with st.container(border=True):
            c_title, c_del = st.columns([5, 1])
            with c_title:
                st.markdown(f"#### ブロック{i+1}")
            with c_del:
                if len(st.session_state.date_blocks) > 1:
                    if st.button("削除", key=f"delete_block_{i}"):
                        st.session_state.date_blocks.pop(i)

                        # 削除後に古い選択状態が残っても実害はないが、画面を即更新する
                        st.rerun()

            ns, ne = mobile_range_calendar(
                f"ブロック{i+1} 期間",
                f"block_{i}",
                s0,
                e0
            )

            new_blocks.append((ns, ne))

            if ns is not None and ne is not None:
                block_workdays = workdays_between(ns, ne)
                selected.extend(block_workdays)

    st.session_state.date_blocks = new_blocks

    if st.button("ブロックを追加", use_container_width=True):
        st.session_state.date_blocks.append((None, None))
        st.rerun()

    selected = sorted(set(selected))

    st.markdown("---")
    st.write(f"選択中の勤務日数: {len(selected)} / {required}")
    if selected:
        st.write(", ".join(d.isoformat() for d in selected))

    note = st.text_input("備考")

    if st.button("保存", use_container_width=True):
        ok, msg = add_or_replace_request(uid, int(rank), selected, note)
        if ok:
            st.success(msg)

            deadline = parse_dt(get_setting("initial_deadline"))
            if datetime.now() > deadline:
                ok2, conflicts = evaluate_dates([(uid, d) for d in selected])
                if ok2:
                    st.info("現時点の既存予定とのコンフリクトはありません。管理者画面で仮確定できます。")
                else:
                    st.warning("既存予定とのコンフリクトがあります。")
                    st.dataframe(conflicts, width="stretch")
            else:
                st.info("初回締切前のため、本判定は締切時点で一括実行されます。")
        else:
            st.error(msg)

    st.markdown("---")
    st.subheader("自分の希望一覧")

    reqs = get_requests(uid)
    if not reqs:
        st.info("まだ希望は登録されていません。")
        return

    for r in reqs:
        dates = r.get("dates", [])
        with st.container(border=True):
            st.markdown(f"**希望ID {r['id']}**　{len(dates)}勤務日")
            if dates:
                st.write(", ".join(dates))
            if r.get("note"):
                st.caption(f"備考: {r['note']}")

            if st.button("この希望を削除", key=f"delete_request_{r['id']}", use_container_width=True):
                delete_request(int(r["id"]))
                st.success("削除しました。")
                st.rerun()



def page_personal_inputs():
    st.header("不在・休暇入力")

    uid = render_user_selector()
    if uid is None:
        return

    user = get_user(uid)
    st.caption(f"選択中: {user['name']}")

    tab_absence, tab_vacation = st.tabs(["夏季休暇以外の不在", "夏季休暇希望"])

    with tab_absence:
        st.subheader("夏季休暇以外の不在を追加")
        st.caption("年休、出張、学会、外勤、研究日などを入力してください。")

        ns, ne = mobile_range_calendar(
            "不在期間",
            f"absence_{uid}",
            None,
            None
        )

        absence_type = st.selectbox(
            "不在種別",
            ["年休", "出張", "学会", "外勤", "研究日", "病休", "その他"],
            key=f"absence_type_{uid}"
        )

        counts = st.checkbox(
            "臨床不在としてカウントする",
            value=True,
            key=f"absence_counts_{uid}"
        )

        desc = st.text_input("備考", key=f"absence_desc_{uid}")

        if st.button("この不在を保存", use_container_width=True, key=f"save_absence_{uid}"):
            if ns is None or ne is None:
                st.error("開始日と終了日を選択してください。")
            elif ne < ns:
                st.error("終了日は開始日以降にしてください。")
            else:
                add_absence(uid, ns, ne, absence_type, desc, counts)
                st.success("不在を保存しました。")
                st.rerun()

        st.markdown("---")
        st.subheader("登録済みの不在")

        rows = get_absences(uid)
        if not rows:
            st.info("登録済みの不在はありません。")
        else:
            for r in rows:
                with st.container(border=True):
                    st.markdown(
                        f"**{r['absence_type']}**　"
                        f"{r['start_date']} 〜 {r['end_date']}"
                    )
                    if r.get("description"):
                        st.caption(f"備考: {r['description']}")
                    st.caption(
                        "臨床不在としてカウント: "
                        + ("はい" if int(r["counts_as_unavailable"]) == 1 else "いいえ")
                    )

                    if st.button(
                        "この不在を削除",
                        key=f"delete_absence_combined_{r['id']}",
                        use_container_width=True
                    ):
                        delete_absence(int(r["id"]))
                        st.success("削除しました。")
                        st.rerun()

    with tab_vacation:
        st.subheader("夏季休暇希望を追加")

        required = int(get_setting("required_workdays"))
        st.info(
            f"勤務日を選択してください。複数回に分けて入力できます。"
            f"合計{required}勤務日まで保存できます。土日と登録済み非勤務日は自動的に除外されます。"
        )

        blocks_key = f"date_blocks_{uid}"
        if blocks_key not in st.session_state:
            st.session_state[blocks_key] = [(None, None)]

        new_blocks = []
        selected = []

        for i, (s0, e0) in enumerate(st.session_state[blocks_key]):
            with st.container(border=True):
                c_title, c_del = st.columns([5, 1])
                with c_title:
                    st.markdown(f"#### ブロック{i+1}")
                with c_del:
                    if len(st.session_state[blocks_key]) > 1:
                        if st.button("削除", key=f"delete_vacation_block_{uid}_{i}"):
                            st.session_state[blocks_key].pop(i)
                            st.rerun()

                ns, ne = mobile_range_calendar(
                    f"ブロック{i+1} 期間",
                    f"vacation_{uid}_block_{i}",
                    s0,
                    e0
                )

                new_blocks.append((ns, ne))

                if ns is not None and ne is not None:
                    block_workdays = workdays_between(ns, ne)
                    selected.extend(block_workdays)

        st.session_state[blocks_key] = new_blocks

        if st.button("ブロックを追加", use_container_width=True, key=f"add_vacation_block_{uid}"):
            st.session_state[blocks_key].append((None, None))
            st.rerun()

        selected = sorted(set(selected))

        st.markdown("---")
        st.write(f"選択中の勤務日数: {len(selected)} / {required}")
        if selected:
            st.write(", ".join(d.isoformat() for d in selected))

        note = st.text_input("備考", key=f"vacation_note_{uid}")

        if st.button("この夏季休暇希望を保存", use_container_width=True, key=f"save_vacation_{uid}"):
            if not selected:
                st.error("休暇日を選択してください。")
            else:
                ok, msg = add_or_replace_request(uid, 1, selected, note)
                if ok:
                    st.success(msg)

                    deadline = parse_dt(get_setting("initial_deadline"))
                    if datetime.now() > deadline:
                        ok2, conflicts = evaluate_dates([(uid, d) for d in selected])
                        if ok2:
                            st.info("現時点の既存予定とのコンフリクトはありません。管理者画面で仮確定できます。")
                        else:
                            st.warning("既存予定とのコンフリクトがあります。")
                            st.dataframe(conflicts, width="stretch")
                    else:
                        st.info("初回締切前のため、本判定は締切時点で一括実行されます。")
                else:
                    st.error(msg)

        st.markdown("---")
        st.subheader("登録済みの夏季休暇希望")

        reqs = get_requests(uid)
        if not reqs:
            st.info("まだ夏季休暇希望は登録されていません。")
        else:
            for r in reqs:
                dates = r.get("dates", [])
                with st.container(border=True):
                    st.markdown(f"**希望ID {r['id']}**　{len(dates)}勤務日")
                    if dates:
                        st.write(", ".join(dates))
                    if r.get("note"):
                        st.caption(f"備考: {r['note']}")

                    if st.button(
                        "この夏季休暇希望を削除",
                        key=f"delete_request_combined_{r['id']}",
                        use_container_width=True
                    ):
                        delete_request(int(r["id"]))
                        st.success("削除しました。")
                        st.rerun()


def page_batch_review():
    st.header("締切時点の一括判定・確定")

    st.warning("この操作は、提出済み希望を申請順ではなく同時評価します。コンフリクトがある場合は確定しません。")
    if st.button("一括判定を実行"):
        clear_tentative_assignments()
        run_id = reset_conflicts()

        ok, conflicts, selected = find_batch_solution()

        if ok:
            pattern_ids = []
            for uid, pid, dates in selected:
                pattern_ids.append(pid)
                for d in dates:
                    add_assignment(uid, d, pid, STATUS_TENTATIVE)
            set_pattern_statuses(pattern_ids, STATUS_TENTATIVE)
            st.success("制約違反のない組み合わせが見つかりました。仮確定しました。")
            st.dataframe(get_assignments([STATUS_TENTATIVE]), use_container_width=True)
        else:
            for c in conflicts:
                add_conflict(run_id, c["date"], c["type"], c["detail"], c["involved"])
            st.error("コンフリクトがあります。確定できません。")
            st.dataframe(get_conflicts(open_only=True), use_container_width=True)

    st.subheader("仮確定一覧")
    tent = get_assignments([STATUS_TENTATIVE])
    if tent:
        st.dataframe(tent, use_container_width=True)
        if st.button("仮確定を正式確定する"):
            confirm_tentative_assignments()
            st.success("正式確定しました。")

    st.subheader("コンフリクト一覧")
    conflicts = get_conflicts(open_only=True)
    if conflicts:
        rows = []
        for c in conflicts:
            row = dict(c)
            row["involved_names"] = conflict_user_names(c)
            rows.append(row)
        st.dataframe(rows, use_container_width=True)

        message = build_conflict_message(conflicts)
        st.text_area("通知文", message, height=200)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Slackに通知"):
                ok, msg = send_slack(message)
                st.success(msg) if ok else st.error(msg)
        with c2:
            if st.button("管理者メール送信用に文面を表示"):
                st.code(message)


def page_assignments():
    st.header("確定・仮確定予定")
    rows = get_assignments()
    if rows:
        st.dataframe(rows, use_container_width=True)
        aid = st.number_input("削除するassignment id", min_value=0, step=1)
        if st.button("予定を削除"):
            delete_assignment(int(aid))
            st.success("削除しました。")
    else:
        st.info("予定はありません。")


def page_dashboard():
    st.header("日別稼働状況ダッシュボード")
    season_start = parse_date(get_setting("season_start"))
    season_end = parse_date(get_setting("season_end"))
    start = st.date_input("表示開始", season_start)
    end = st.date_input("表示終了", min(season_end, season_start + timedelta(days=30)))

    users = get_users()
    user_map = {u["id"]: u for u in users}
    unavailable = current_unavailable_dates()

    rows = []
    for d in workdays_between(start, end):
        off_ids = [uid for (uid, dd), reason in unavailable.items() if dd == d]
        staff_off = [uid for uid in off_ids if user_map.get(uid, {}).get("category") == USER_STAFF]

        chief_present = 0
        clinical_present = 0
        total_res_present = 0
        for u in users:
            if u["category"] != USER_RESIDENT or not u["active"]:
                continue
            role = role_on_date(u["id"], d)
            if not role:
                continue
            if u["id"] in off_ids:
                continue
            if role == ROLE_CHIEF:
                chief_present += 1
            if role in (ROLE_CHIEF, ROLE_CLINICAL):
                clinical_present += 1
            if role in (ROLE_CHIEF, ROLE_CLINICAL, ROLE_PATHOLOGY):
                total_res_present += 1

        rows.append({
            "date": d.isoformat(),
            "staff_off": len(staff_off),
            "staff_off_names": ", ".join(user_map[i]["name"] for i in staff_off if i in user_map),
            "chief_present": chief_present,
            "chief_clinical_present": clinical_present,
            "total_resident_present": total_res_present,
            "off_names": ", ".join(user_map[i]["name"] for i in off_ids if i in user_map)
        })

    st.dataframe(rows, use_container_width=True)


def main():
    st.set_page_config(page_title="夏季休暇調整アプリ", layout="wide")
    init_db()

    st.title("夏季休暇調整アプリ")
    st.caption("初回締切時に全希望を同時評価し、締切後は既存予定とのコンフリクトを随時判定します。")

    st.sidebar.markdown("## ログイン")

    admin_password = st.sidebar.text_input(
        "管理者パスワード",
        type="password"
    )

    is_admin = admin_password == "admin123"

    if is_admin:
        pages = {
            "管理者設定": page_admin_settings,
            "ユーザー管理": page_users,
            "レジデント役割": page_roles,
            "不在・休暇入力": page_personal_inputs,
            "一括判定・確定": page_batch_review,
            "予定一覧": page_assignments,
            "稼働状況": page_dashboard,
        }
    else:
        pages = {
            "不在・休暇入力": page_personal_inputs,
            "予定一覧": page_assignments,
        }

    choice = st.sidebar.radio("メニュー", list(pages.keys()))
    st.sidebar.markdown("---")
    st.sidebar.write("対象期間:", get_setting("season_start"), "〜", get_setting("season_end"))
    st.sidebar.write("初回締切:", get_setting("initial_deadline"))
    pages[choice]()


if __name__ == "__main__":
    main()
