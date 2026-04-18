import http.server
import json
import sqlite3
import hashlib
import os
import urllib.parse
from datetime import datetime

PORT = 8080
BASE = os.path.dirname(os.path.abspath(__file__))
WEB  = os.path.join(BASE, "frontend")
DB   = os.path.join(BASE, "database.db")

# ── Database Setup ──
def get_db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def setup():
    con = get_db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS department_admins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            department  TEXT NOT NULL UNIQUE,
            username    TEXT NOT NULL UNIQUE,
            password    TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS mentors (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            department TEXT NOT NULL DEFAULT 'Computer Engineering',
            subject    TEXT NOT NULL,
            password   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schedule (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            day     TEXT NOT NULL,
            subject TEXT NOT NULL,
            time    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            student TEXT DEFAULT '',
            roll_no TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            date    TEXT DEFAULT '',
            status  TEXT DEFAULT 'present',
            UNIQUE(roll_no, subject, date)
        );
        CREATE TABLE IF NOT EXISTS students (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            roll_no    TEXT NOT NULL UNIQUE,
            department TEXT NOT NULL DEFAULT 'Computer Engineering',
            password   TEXT NOT NULL
        );
    """)

    # ── Migrate attendance table if the old UNIQUE constraint exists ──
    # The old schema had UNIQUE(student, subject, date).
    # We detect this by inspecting the table's CREATE sql in sqlite_master.
    needs_migration = False
    tbl_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='attendance'"
    ).fetchone()
    if tbl_row:
        tsql = (tbl_row["sql"] or "").replace(" ", "").lower()
        # Old schema: unique(student,subject,date) — no roll_no in the unique clause
        if "unique(student,subject,date)" in tsql:
            needs_migration = True

    if needs_migration:
        print("  Migrating attendance table schema...")
        con.executescript("""
            ALTER TABLE attendance RENAME TO attendance_old;
            CREATE TABLE attendance (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                student TEXT DEFAULT '',
                roll_no TEXT DEFAULT '',
                subject TEXT DEFAULT '',
                date    TEXT DEFAULT '',
                status  TEXT DEFAULT 'present',
                UNIQUE(roll_no, subject, date)
            );
            INSERT OR IGNORE INTO attendance(id, student, roll_no, subject, date, status)
            SELECT id, student, roll_no, subject, date, status FROM attendance_old;
            DROP TABLE attendance_old;
        """)
        print("  Attendance table migrated successfully.")

    # ── Safe column migrations ──
    mentor_cols = [r["name"] for r in con.execute("PRAGMA table_info(mentors)").fetchall()]
    if "department" not in mentor_cols:
        con.execute("ALTER TABLE mentors ADD COLUMN department TEXT DEFAULT 'Computer Engineering'")

    student_cols = [r["name"] for r in con.execute("PRAGMA table_info(students)").fetchall()]
    if "department" not in student_cols:
        con.execute("ALTER TABLE students ADD COLUMN department TEXT DEFAULT 'Computer Engineering'")

    attendance_cols = [r["name"] for r in con.execute("PRAGMA table_info(attendance)").fetchall()]
    if "student" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN student TEXT DEFAULT ''")
    if "roll_no" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN roll_no TEXT DEFAULT ''")
    if "subject" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN subject TEXT DEFAULT ''")
    if "date" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN date TEXT DEFAULT ''")
    if "status" not in attendance_cols:
        con.execute("ALTER TABLE attendance ADD COLUMN status TEXT DEFAULT 'present'")

    # ── Seed default admin ──
    dept_admin_count = con.execute("SELECT COUNT(*) FROM department_admins").fetchone()[0]
    if dept_admin_count == 0:
        con.execute("""
            INSERT INTO department_admins(department, username, password, is_active)
            VALUES(?, ?, ?, 1)
        """, ("Computer Engineering", "ce_admin", pw("admin123")))
        print("  Default Computer Engineering admin added")

    # ── Seed default schedule ──
    count = con.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
    if count == 0:
        default = [
            ("Monday",    "Math",      "10:00 AM"),
            ("Tuesday",   "Physics",   "11:00 AM"),
            ("Wednesday", "Chemistry", "10:00 AM"),
            ("Thursday",  "FEEE",      "12:00 PM"),
            ("Friday",    "Math",      "9:00 AM"),
            ("Saturday",  "Physics",   "11:30 AM"),
            ("Sunday",    "Holiday",   "-"),
        ]
        con.executemany("INSERT INTO schedule(day, subject, time) VALUES(?, ?, ?)", default)
        print("  Default schedule added")

    con.commit()
    con.close()


def pw(text):
    return hashlib.sha256(text.encode()).hexdigest()

def rows(r):
    return [dict(x) for x in r]


# ── API handlers ──

def login_mentor(body):
    name       = body.get("name", "").strip()
    department = body.get("department", "Computer Engineering").strip()
    password   = body.get("password", "")
    con = get_db()
    m = con.execute("""
        SELECT * FROM mentors
        WHERE LOWER(name)=LOWER(?)
          AND LOWER(department)=LOWER(?)
          AND password=?
    """, (name, department, pw(password))).fetchone()
    con.close()
    if not m:
        return 401, {"error": "Wrong name or password"}
    return 200, {"mentor": dict(m)}

def get_mentors():
    con = get_db()
    data = rows(con.execute("SELECT * FROM mentors ORDER BY department, name").fetchall())
    con.close()
    return 200, data

def get_departments():
    con = get_db()
    data = rows(con.execute("""
        SELECT id, department, username, is_active
        FROM department_admins
        ORDER BY department
    """).fetchall())
    con.close()
    return 200, data

def login_department_admin(body):
    department = body.get("department", "").strip()
    username   = body.get("username", "").strip()
    password   = body.get("password", "")
    if not department or not username or not password:
        return 400, {"error": "department, username and password are required"}
    con = get_db()
    admin = con.execute("""
        SELECT id, department, username
        FROM department_admins
        WHERE LOWER(department)=LOWER(?)
          AND LOWER(username)=LOWER(?)
          AND password=?
          AND is_active=1
    """, (department, username, pw(password))).fetchone()
    con.close()
    if not admin:
        return 401, {"error": "Wrong department admin credentials"}
    return 200, {"admin": dict(admin)}

def add_mentor(body):
    name       = body.get("name", "").strip()
    department = body.get("department", "Computer Engineering").strip()
    subject    = body.get("subject", "").strip()
    password   = body.get("password", "").strip()
    if not name or not department or not subject or not password:
        return 400, {"error": "All fields are required"}
    con = get_db()
    try:
        con.execute(
            "INSERT INTO mentors(name, department, subject, password) VALUES(?, ?, ?, ?)",
            (name, department, subject, pw(password))
        )
        con.commit()
        return 201, {"message": "Mentor added"}
    except Exception as e:
        return 500, {"error": str(e)}
    finally:
        con.close()

def delete_mentor(mid):
    con = get_db()
    con.execute("DELETE FROM mentors WHERE id=?", (mid,))
    con.commit()
    con.close()
    return 200, {"message": "Deleted"}

def get_schedule(day=None):
    con = get_db()
    if day:
        data = rows(con.execute("SELECT * FROM schedule WHERE day=?", (day,)).fetchall())
    else:
        data = rows(con.execute("""
            SELECT * FROM schedule ORDER BY
            CASE day
                WHEN 'Monday'    THEN 1
                WHEN 'Tuesday'   THEN 2
                WHEN 'Wednesday' THEN 3
                WHEN 'Thursday'  THEN 4
                WHEN 'Friday'    THEN 5
                WHEN 'Saturday'  THEN 6
                ELSE 7
            END
        """).fetchall())
    con.close()
    return 200, data

def update_schedule(body):
    sid     = body.get("id")
    subject = body.get("subject", "").strip()
    time    = body.get("time", "").strip()
    if not sid or not subject or not time:
        return 400, {"error": "id, subject and time required"}
    con = get_db()
    con.execute("UPDATE schedule SET subject=?, time=? WHERE id=?", (subject, time, sid))
    con.commit()
    con.close()
    return 200, {"message": "Updated"}

def mark_attendance(body):
    student = body.get("student", "").strip()
    roll_no = body.get("roll_no", "").strip().upper()
    subject = body.get("subject", "").strip()
    status  = body.get("status", "present")
    date    = body.get("date", datetime.now().strftime("%Y-%m-%d"))

    if not student or not subject:
        return 400, {"error": "student and subject required"}

    con = get_db()
    try:
        con.execute("""
            INSERT INTO attendance(student, roll_no, subject, date, status)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(roll_no, subject, date)
            DO UPDATE SET
                student = excluded.student,
                status  = excluded.status
        """, (student, roll_no, subject, date, status))
        con.commit()
        return 200, {"message": "Saved"}
    except Exception as e:
        return 500, {"error": str(e)}
    finally:
        con.close()

def get_attendance(subject=None, date=None):
    con = get_db()
    q = "SELECT * FROM attendance WHERE 1=1"
    p = []
    if subject:
        q += " AND subject=?"
        p.append(subject)
    if date:
        q += " AND date=?"
        p.append(date)
    q += " ORDER BY date DESC"
    data = rows(con.execute(q, p).fetchall())
    con.close()
    return 200, data

def get_students():
    con = get_db()
    data = rows(con.execute(
        "SELECT id, name, roll_no, department FROM students ORDER BY roll_no"
    ).fetchall())
    con.close()
    return 200, data

def add_student(body):
    name       = body.get("name", "").strip()
    roll_no    = body.get("roll_no", "").strip().upper()
    department = body.get("department", "Computer Engineering").strip()
    password   = body.get("password", "").strip()
    if not name or not roll_no or not department or not password:
        return 400, {"error": "All fields required"}
    con = get_db()
    try:
        con.execute(
            "INSERT INTO students(name, roll_no, department, password) VALUES(?, ?, ?, ?)",
            (name, roll_no, department, pw(password))
        )
        con.commit()
        return 201, {"message": "Student added"}
    except sqlite3.IntegrityError:
        return 409, {"error": "Roll number already exists"}
    finally:
        con.close()

def delete_student(sid):
    con = get_db()
    con.execute("DELETE FROM students WHERE id=?", (sid,))
    con.commit()
    con.close()
    return 200, {"message": "Deleted"}

def login_student(body):
    roll_no  = body.get("roll_no", "").strip().upper()
    password = body.get("password", "")
    con = get_db()
    s = con.execute(
        "SELECT * FROM students WHERE roll_no=? AND password=?",
        (roll_no, pw(password))
    ).fetchone()
    con.close()
    if not s:
        return 401, {"error": "Wrong roll number or password"}
    return 200, {"student": dict(s)}


# ── HTTP Server ──
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args): pass

    def send_json(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def send_file(self, path):
        if not os.path.exists(path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        ext  = os.path.splitext(path)[1]
        mime = {
            ".html": "text/html",
            ".css":  "text/css",
            ".js":   "application/javascript"
        }.get(ext, "text/plain")
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def get_path(self):
        return urllib.parse.urlparse(self.path).path

    def get_query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def get_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            path  = self.get_path()
            query = self.get_query()

            if not path.startswith("/api"):
                name = "index.html" if path == "/" else path.lstrip("/")
                self.send_file(os.path.join(WEB, name))
                return

            if   path == "/api/mentors":
                code, data = get_mentors()
            elif path == "/api/departments":
                code, data = get_departments()
            elif path == "/api/students":
                code, data = get_students()
            elif path == "/api/schedule":
                code, data = get_schedule(query.get("day"))
            elif path == "/api/attendance":
                code, data = get_attendance(query.get("subject"), query.get("date"))
            else:
                code, data = 404, {"error": "Not found"}
        except Exception as e:
            code, data = 500, {"error": f"Server error: {str(e)}"}
        self.send_json(code, data)

    def do_POST(self):
        try:
            path = self.get_path()
            body = self.get_body()

            if   path == "/api/admin/login":
                code, data = login_department_admin(body)
            elif path == "/api/mentors/login":
                code, data = login_mentor(body)
            elif path == "/api/students/login":
                code, data = login_student(body)
            elif path == "/api/students":
                code, data = add_student(body)
            elif path == "/api/mentors":
                code, data = add_mentor(body)
            elif path == "/api/schedule":
                code, data = update_schedule(body)
            elif path == "/api/attendance":
                code, data = mark_attendance(body)
            else:
                code, data = 404, {"error": "Not found"}
        except Exception as e:
            code, data = 500, {"error": f"Server error: {str(e)}"}
        self.send_json(code, data)

    def do_DELETE(self):
        try:
            parts = self.get_path().strip("/").split("/")
            if len(parts) == 3 and parts[1] == "mentors":
                code, data = delete_mentor(parts[2])
            elif len(parts) == 3 and parts[1] == "students":
                code, data = delete_student(parts[2])
            else:
                code, data = 404, {"error": "Not found"}
        except Exception as e:
            code, data = 500, {"error": f"Server error: {str(e)}"}
        self.send_json(code, data)


if __name__ == "__main__":
    setup()

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    print("=" * 45)
    print("  College Attendance Management System")
    print("=" * 45)
    print(f"  Open in browser : http://localhost:{PORT}")
    print(f"  Mobile / Hotspot: http://{ip}:{PORT}")
    print("  Stop server     : Ctrl + C")
    print("=" * 45)

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")