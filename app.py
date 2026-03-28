import os
import sqlite3
import json
from datetime import datetime
from io import BytesIO
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
DB_PATH = os.path.join(os.path.dirname(__file__), "scores.db")
APP_PIN = os.environ.get("APP_PIN", "0000")
APP_URL = os.environ.get("APP_URL", "http://localhost:8080")

# Load config from config.json if it exists, otherwise use generic examples
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        _config = json.load(f)
    DEFAULT_TEAMS = [(t["name"], t.get("active", 0)) for t in _config.get("teams", [])]
    DEFAULT_JUDGES = _config.get("judges", ["Judge 1", "Judge 2", "Judge 3"])
    TECH_EVAL = _config.get("tech_eval", {})
    APP_PIN = _config.get("pin", APP_PIN)
    APP_URL = _config.get("app_url", APP_URL)
else:
    DEFAULT_TEAMS = [("Team A", 1), ("Team B", 1), ("Team C", 1)]
    DEFAULT_JUDGES = ["Judge 1", "Judge 2", "Judge 3"]
    TECH_EVAL = {}

CRITERIA = [
    ("mehrwert_idee", "Mehrwert der Idee"),
    ("innovationsgrad", "Innovationsgrad"),
    ("informationsgrad", "Informationsgrad"),
    ("umsetzbarkeit", "Umsetzbarkeit"),
    ("qualitaet_pitch", "Qualität des Pitches"),
]

MAX_SCORE = 4


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            judge_name TEXT NOT NULL,
            team_name TEXT NOT NULL,
            mehrwert_idee INTEGER,
            innovationsgrad INTEGER,
            informationsgrad INTEGER,
            umsetzbarkeit INTEGER,
            qualitaet_pitch INTEGER,
            kommentar TEXT DEFAULT '',
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            PRIMARY KEY (judge_name, team_name, version)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            team_name TEXT PRIMARY KEY,
            active INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS judges (
            judge_name TEXT PRIMARY KEY,
            sort_order INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Seed default teams if table is empty
    existing = conn.execute("SELECT COUNT(*) as c FROM teams").fetchone()["c"]
    if existing == 0:
        for i, (name, active) in enumerate(DEFAULT_TEAMS):
            conn.execute("INSERT INTO teams (team_name, active, sort_order) VALUES (?, ?, ?)", (name, active, i))
    # Seed default judges if table is empty
    existing_j = conn.execute("SELECT COUNT(*) as c FROM judges").fetchone()["c"]
    if existing_j == 0:
        for i, j in enumerate(DEFAULT_JUDGES):
            conn.execute("INSERT INTO judges (judge_name, sort_order) VALUES (?, ?)", (j, i))
    # Ensure current version exists
    v = conn.execute("SELECT value FROM app_state WHERE key='current_version'").fetchone()
    if not v:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('current_version', '1')")
    conn.commit()
    conn.close()


def get_current_version():
    conn = get_db()
    row = conn.execute("SELECT value FROM app_state WHERE key='current_version'").fetchone()
    conn.close()
    return int(row["value"]) if row else 1
    conn.commit()
    conn.close()


def get_all_teams():
    conn = get_db()
    rows = conn.execute("SELECT team_name FROM teams ORDER BY sort_order, team_name").fetchall()
    conn.close()
    return [r["team_name"] for r in rows]


def get_active_teams():
    conn = get_db()
    rows = conn.execute("SELECT team_name FROM teams WHERE active=1 ORDER BY sort_order, team_name").fetchall()
    conn.close()
    if not rows:
        return get_all_teams()
    return [r["team_name"] for r in rows]


def get_judges():
    conn = get_db()
    rows = conn.execute("SELECT judge_name FROM judges ORDER BY sort_order, judge_name").fetchall()
    conn.close()
    return [r["judge_name"] for r in rows]


@app.before_request
def require_pin():
    open_paths = ['/login', '/api/pin-check', '/api/backup', '/api/restore', '/static/']
    if any(request.path.startswith(p) for p in open_paths):
        return
    if not session.get('authenticated'):
        return redirect(url_for('login'))


@app.context_processor
def inject_globals():
    return {"app_url": APP_URL}


@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html", app_url=APP_URL)


@app.route("/api/pin-check", methods=["POST"])
def pin_check():
    data = request.json
    if data.get("pin") == APP_PIN:
        session['authenticated'] = True
        session.permanent = True
        return jsonify({"status": "ok"})
    return jsonify({"error": "Falscher PIN"}), 401


@app.route("/")
def index():
    return render_template("index.html", judges=get_judges(), teams=get_active_teams(),
                           all_teams=get_all_teams(), criteria=CRITERIA, max_score=MAX_SCORE,
                           tech_eval=TECH_EVAL)


@app.route("/evaluate/<judge>")
def evaluate(judge):
    judges = get_judges()
    if judge not in judges:
        return "Unbekannter Juror", 404
    return render_template("index.html", judges=judges, teams=get_active_teams(),
                           all_teams=get_all_teams(), criteria=CRITERIA, current_judge=judge,
                           max_score=MAX_SCORE, tech_eval=TECH_EVAL)


@app.route("/dashboard")
def dashboard():
    return render_template("index.html", judges=get_judges(), teams=get_active_teams(),
                           all_teams=get_all_teams(), criteria=CRITERIA, view="dashboard",
                           max_score=MAX_SCORE, tech_eval=TECH_EVAL)


@app.route("/admin")
def admin():
    return render_template("index.html", judges=get_judges(), teams=get_active_teams(),
                           all_teams=get_all_teams(), criteria=CRITERIA, view="admin",
                           current_version=get_current_version(), max_score=MAX_SCORE,
                           tech_eval=TECH_EVAL)


@app.route("/api/score", methods=["POST"])
def save_score():
    data = request.json
    judge = data.get("judge_name")
    team = data.get("team_name")
    if judge not in get_judges():
        return jsonify({"error": "Invalid judge"}), 400
    all_teams = get_all_teams()
    if team not in all_teams:
        return jsonify({"error": "Invalid team"}), 400

    conn = get_db()
    ver = get_current_version()
    conn.execute("""
        INSERT INTO scores (judge_name, team_name, mehrwert_idee, innovationsgrad,
                           informationsgrad, umsetzbarkeit, qualitaet_pitch, kommentar, updated_at, version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(judge_name, team_name, version) DO UPDATE SET
            mehrwert_idee=excluded.mehrwert_idee,
            innovationsgrad=excluded.innovationsgrad,
            informationsgrad=excluded.informationsgrad,
            umsetzbarkeit=excluded.umsetzbarkeit,
            qualitaet_pitch=excluded.qualitaet_pitch,
            kommentar=excluded.kommentar,
            updated_at=excluded.updated_at
    """, (
        judge, team,
        data.get("mehrwert_idee"),
        data.get("innovationsgrad"),
        data.get("informationsgrad"),
        data.get("umsetzbarkeit"),
        data.get("qualitaet_pitch"),
        data.get("kommentar", ""),
        datetime.now().isoformat(),
        ver
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "saved"})


@app.route("/api/scores")
def get_all_scores():
    conn = get_db()
    ver = get_current_version()
    rows = conn.execute("SELECT * FROM scores WHERE version=? ORDER BY judge_name, team_name", (ver,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/scores/<judge>")
def get_judge_scores(judge):
    conn = get_db()
    ver = get_current_version()
    rows = conn.execute("SELECT * FROM scores WHERE judge_name=? AND version=?", (judge, ver)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/dashboard")
def dashboard_data():
    conn = get_db()
    ver = get_current_version()
    rows = conn.execute("SELECT * FROM scores WHERE version=?", (ver,)).fetchall()
    conn.close()

    team_scores = {}
    judges = get_judges()
    judge_progress = {j: 0 for j in judges}

    for r in rows:
        r = dict(r)
        team = r["team_name"]
        judge = r["judge_name"]
        total = sum(r.get(c[0], 0) or 0 for c in CRITERIA)

        if team not in team_scores:
            team_scores[team] = {"totals": [], "criteria": {c[0]: [] for c in CRITERIA}}
        team_scores[team]["totals"].append(total)
        for c_key, _ in CRITERIA:
            if r.get(c_key) is not None:
                team_scores[team]["criteria"][c_key].append(r[c_key])

        judge_progress[judge] = judge_progress.get(judge, 0) + 1

    leaderboard = []
    active = get_active_teams()
    for team in active:
        entry = {"team": team, "avg_total": 0, "num_judges": 0, "criteria_avg": {}}
        if team in team_scores:
            ts = team_scores[team]
            entry["avg_total"] = round(sum(ts["totals"]) / len(ts["totals"]), 1)
            entry["num_judges"] = len(ts["totals"])
            for c_key, _ in CRITERIA:
                vals = ts["criteria"][c_key]
                entry["criteria_avg"][c_key] = round(sum(vals) / len(vals), 1) if vals else 0
        leaderboard.append(entry)

    leaderboard.sort(key=lambda x: -x["avg_total"])
    return jsonify({"leaderboard": leaderboard, "judge_progress": judge_progress,
                     "total_teams": len(active)})


@app.route("/api/export")
def export_excel():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Präsentationsbewertung"
    active = get_active_teams()
    judges = get_judges()

    header_fill = PatternFill("solid", fgColor="1a365d")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin")
    )

    headers = ["Team"] + [f"{j}\nTotal" for j in judges] + ["Durchschnitt"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = thin_border

    conn = get_db()
    ver = get_current_version()
    rows = conn.execute("SELECT * FROM scores WHERE version=?", (ver,)).fetchall()
    conn.close()

    score_map = {}
    for r in rows:
        r = dict(r)
        key = (r["judge_name"], r["team_name"])
        total = sum(r.get(c[0], 0) or 0 for c in CRITERIA)
        score_map[key] = {"total": total, "kommentar": r.get("kommentar", "")}

    for row_idx, team in enumerate(active, 2):
        ws.cell(row=row_idx, column=1, value=team).border = thin_border
        totals = []
        for j_idx, judge in enumerate(judges, 2):
            entry = score_map.get((judge, team))
            val = entry["total"] if entry else None
            cell = ws.cell(row=row_idx, column=j_idx, value=val)
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border
            if val is not None:
                totals.append(val)
        avg = round(sum(totals) / len(totals), 1) if totals else None
        cell = ws.cell(row=row_idx, column=len(judges) + 2, value=avg)
        cell.alignment = Alignment(horizontal="center")
        cell.border = thin_border

    ws.column_dimensions["A"].width = 22
    for col in range(2, len(judges) + 3):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 14

    # Detail sheet per judge
    for judge in judges:
        js = wb.create_sheet(title=judge)
        detail_headers = ["Team"] + [c[1] for c in CRITERIA] + ["Total", "Kommentar"]
        for col, h in enumerate(detail_headers, 1):
            cell = js.cell(row=1, column=col, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", wrap_text=True)

        for row_idx, team in enumerate(active, 2):
            js.cell(row=row_idx, column=1, value=team)
            conn = get_db()
            r = conn.execute("SELECT * FROM scores WHERE judge_name=? AND team_name=?",
                             (judge, team)).fetchone()
            conn.close()
            if r:
                r = dict(r)
                total = 0
                for c_idx, (c_key, _) in enumerate(CRITERIA, 2):
                    val = r.get(c_key) or 0
                    js.cell(row=row_idx, column=c_idx, value=val)
                    total += val
                js.cell(row=row_idx, column=len(CRITERIA) + 2, value=total)
                js.cell(row=row_idx, column=len(CRITERIA) + 3, value=r.get("kommentar", ""))

        js.column_dimensions["A"].width = 22
        for col in range(2, len(CRITERIA) + 4):
            js.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 16

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, download_name="BadenHackt2026_Praesentation_Bewertung.xlsx",
                     as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/active-teams", methods=["GET"])
def get_active_teams_api():
    return jsonify({"active": get_active_teams(), "all": get_all_teams()})


@app.route("/api/active-teams", methods=["POST"])
def set_active_teams():
    data = request.json
    teams = data.get("teams", [])
    conn = get_db()
    conn.execute("UPDATE teams SET active=0")
    for t in teams:
        conn.execute("UPDATE teams SET active=1 WHERE team_name=?", (t,))
    conn.commit()
    conn.close()
    return jsonify({"status": "saved", "active": len(teams)})


@app.route("/api/teams", methods=["POST"])
def add_team():
    data = request.json
    name = data.get("team_name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 as n FROM teams").fetchone()["n"]
    try:
        conn.execute("INSERT INTO teams (team_name, active, sort_order) VALUES (?, 1, ?)", (name, max_order))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Team already exists"}), 409
    conn.close()
    return jsonify({"status": "added", "team_name": name})


@app.route("/api/teams/<path:team_name>", methods=["DELETE"])
def delete_team(team_name):
    conn = get_db()
    conn.execute("DELETE FROM teams WHERE team_name=?", (team_name,))
    conn.execute("DELETE FROM scores WHERE team_name=?", (team_name,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/teams/rename", methods=["POST"])
def rename_team():
    data = request.json
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "Both old_name and new_name required"}), 400
    conn = get_db()
    conn.execute("UPDATE teams SET team_name=? WHERE team_name=?", (new_name, old_name))
    conn.execute("UPDATE scores SET team_name=? WHERE team_name=?", (new_name, old_name))
    conn.commit()
    conn.close()
    return jsonify({"status": "renamed", "old": old_name, "new": new_name})


@app.route("/api/version")
def get_version():
    ver = get_current_version()
    conn = get_db()
    versions = conn.execute(
        "SELECT DISTINCT version FROM scores ORDER BY version"
    ).fetchall()
    conn.close()
    return jsonify({
        "current": ver,
        "all": [r["version"] for r in versions] if versions else [ver]
    })


@app.route("/api/archive", methods=["POST"])
def archive_and_new():
    ver = get_current_version()
    new_ver = ver + 1
    conn = get_db()
    conn.execute("UPDATE app_state SET value=? WHERE key='current_version'", (str(new_ver),))
    conn.commit()
    conn.close()
    return jsonify({"status": "archived", "old_version": ver, "new_version": new_ver})


@app.route("/api/scores/version/<int:ver>")
def get_version_scores(ver):
    conn = get_db()
    rows = conn.execute("SELECT * FROM scores WHERE version=? ORDER BY judge_name, team_name", (ver,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tech-eval")
def tech_eval_api():
    return jsonify(TECH_EVAL)


@app.route("/api/backup")
def backup_state():
    conn = get_db()
    teams = [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY sort_order").fetchall()]
    judges = [dict(r) for r in conn.execute("SELECT * FROM judges ORDER BY sort_order").fetchall()]
    scores = [dict(r) for r in conn.execute("SELECT * FROM scores ORDER BY version, judge_name, team_name").fetchall()]
    state = [dict(r) for r in conn.execute("SELECT * FROM app_state").fetchall()]
    conn.close()
    return jsonify({"teams": teams, "judges": judges, "scores": scores, "app_state": state})


@app.route("/api/restore", methods=["POST"])
def restore_state():
    data = request.json
    conn = get_db()
    # Clear existing
    conn.execute("DELETE FROM scores")
    conn.execute("DELETE FROM teams")
    conn.execute("DELETE FROM judges")
    conn.execute("DELETE FROM app_state")
    # Restore teams
    for t in data.get("teams", []):
        conn.execute("INSERT OR REPLACE INTO teams (team_name, active, sort_order) VALUES (?, ?, ?)",
                     (t["team_name"], t.get("active", 0), t.get("sort_order", 0)))
    # Restore judges
    for j in data.get("judges", []):
        conn.execute("INSERT OR REPLACE INTO judges (judge_name, sort_order) VALUES (?, ?)",
                     (j["judge_name"], j.get("sort_order", 0)))
    # Restore scores
    for s in data.get("scores", []):
        conn.execute("""INSERT OR REPLACE INTO scores
            (judge_name, team_name, mehrwert_idee, innovationsgrad, informationsgrad,
             umsetzbarkeit, qualitaet_pitch, kommentar, updated_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s["judge_name"], s["team_name"], s.get("mehrwert_idee"), s.get("innovationsgrad"),
             s.get("informationsgrad"), s.get("umsetzbarkeit"), s.get("qualitaet_pitch"),
             s.get("kommentar", ""), s.get("updated_at"), s.get("version", 1)))
    # Restore app_state
    for st in data.get("app_state", []):
        conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                     (st["key"], st["value"]))
    conn.commit()
    conn.close()
    return jsonify({"status": "restored", "teams": len(data.get("teams", [])),
                     "judges": len(data.get("judges", [])), "scores": len(data.get("scores", []))})


@app.route("/api/judges", methods=["GET"])
def get_judges_api():
    return jsonify(get_judges())


@app.route("/api/judges", methods=["POST"])
def add_judge():
    data = request.json
    name = data.get("judge_name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 as n FROM judges").fetchone()["n"]
    try:
        conn.execute("INSERT INTO judges (judge_name, sort_order) VALUES (?, ?)", (name, max_order))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Judge already exists"}), 409
    conn.close()
    return jsonify({"status": "added", "judge_name": name})


@app.route("/api/judges/<path:judge_name>", methods=["DELETE"])
def delete_judge(judge_name):
    conn = get_db()
    conn.execute("DELETE FROM judges WHERE judge_name=?", (judge_name,))
    conn.execute("DELETE FROM scores WHERE judge_name=?", (judge_name,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/judges/rename", methods=["POST"])
def rename_judge():
    data = request.json
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "Both old_name and new_name required"}), 400
    conn = get_db()
    conn.execute("UPDATE judges SET judge_name=? WHERE judge_name=?", (new_name, old_name))
    conn.execute("UPDATE scores SET judge_name=? WHERE judge_name=?", (new_name, old_name))
    conn.commit()
    conn.close()
    return jsonify({"status": "renamed", "old": old_name, "new": new_name})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
