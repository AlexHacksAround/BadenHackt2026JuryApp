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
LOCAL_BACKUP_PATH = os.path.join(os.path.dirname(__file__), "local_backup.json")
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
            sort_order INTEGER DEFAULT 0,
            jury_factor REAL DEFAULT 1.0
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
    # Migrate: add jury_factor column if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(teams)").fetchall()]
    if 'jury_factor' not in cols:
        conn.execute("ALTER TABLE teams ADD COLUMN jury_factor REAL DEFAULT 1.0")
    conn.commit()
    conn.close()


def get_current_version():
    conn = get_db()
    row = conn.execute("SELECT value FROM app_state WHERE key='current_version'").fetchone()
    conn.close()
    return int(row["value"]) if row else 1


def save_local_backup():
    """Save full DB state to a local JSON file after every mutation."""
    try:
        conn = get_db()
        data = {
            "teams": [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY sort_order").fetchall()],
            "judges": [dict(r) for r in conn.execute("SELECT * FROM judges ORDER BY sort_order").fetchall()],
            "scores": [dict(r) for r in conn.execute("SELECT * FROM scores").fetchall()],
            "app_state": [dict(r) for r in conn.execute("SELECT * FROM app_state").fetchall()],
        }
        conn.close()
        with open(LOCAL_BACKUP_PATH, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def restore_from_local_backup():
    """On startup, if DB is empty but local backup exists, restore from it."""
    if not os.path.exists(LOCAL_BACKUP_PATH):
        return
    conn = get_db()
    score_count = conn.execute("SELECT COUNT(*) as c FROM scores").fetchone()["c"]
    conn.close()
    if score_count > 0:
        return  # DB already has data, don't overwrite
    try:
        with open(LOCAL_BACKUP_PATH) as f:
            data = json.load(f)
        if not data.get("scores"):
            return  # backup is empty too
        conn = get_db()
        for t in data.get("teams", []):
            conn.execute("INSERT OR REPLACE INTO teams (team_name, active, sort_order, jury_factor) VALUES (?, ?, ?, ?)",
                         (t["team_name"], t.get("active", 0), t.get("sort_order", 0), t.get("jury_factor", 1.0)))
        for j in data.get("judges", []):
            conn.execute("INSERT OR REPLACE INTO judges (judge_name, sort_order) VALUES (?, ?)",
                         (j["judge_name"], j.get("sort_order", 0)))
        for s in data.get("scores", []):
            conn.execute("""INSERT OR REPLACE INTO scores
                (judge_name, team_name, mehrwert_idee, innovationsgrad, informationsgrad,
                 umsetzbarkeit, qualitaet_pitch, kommentar, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s["judge_name"], s["team_name"], s.get("mehrwert_idee"), s.get("innovationsgrad"),
                 s.get("informationsgrad"), s.get("umsetzbarkeit"), s.get("qualitaet_pitch"),
                 s.get("kommentar", ""), s.get("updated_at"), s.get("version", 1)))
        for st in data.get("app_state", []):
            conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                         (st["key"], st["value"]))
        conn.commit()
        conn.close()
    except Exception:
        pass
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
    open_paths = ['/login', '/api/pin-check', '/api/backup', '/api/restore', '/static/', '/teams']
    if any(request.path.startswith(p) for p in open_paths):
        return
    if not session.get('authenticated'):
        return redirect(url_for('login'))


@app.context_processor
def inject_globals():
    return {"app_url": APP_URL}


@app.after_request
def auto_backup(response):
    if request.method == 'POST' and response.status_code == 200:
        save_local_backup()
    return response


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


TEAM_FEEDBACK = {
    "ABB TS 1": {
        "achievement": "Ihr habt einen vollständigen Adventskalender mit 24 Quizfragen, QR-Code-Generierung und einem Admin-Panel gebaut. Die Kombination aus React 19, TypeScript und Framer Motion zeigt, dass ihr moderne Webentwicklung beherrscht. Besonders beeindruckend: Jeder Tag hat eigene Farben, eigene Inhalte und eigene Quizfragen — das ist viel Content-Arbeit neben dem Code.",
        "creativity": "Solide Handwerkskunst mit sauberem TypeScript und durchdachter Komponentenstruktur. Ein ausgewogenes Projekt ohne offensichtliche Schwächen.",
        "improve": ["Verbindet den QR-Code-Scan mit dem Quiz-Flow — die Bausteine sind da, der letzte Schritt fehlt", "Fügt eine Bestenliste hinzu, damit Schüler gegeneinander antreten können", "Ein einfaches Backend (z.B. Supabase) würde Ergebnisse persistent machen"]
    },
    "ABB TS 2": {
        "achievement": "Die fliegende Weihnachtsschlitten-Animation und der Schneefall-Effekt zeigen echtes visuelles Talent. Ihr habt mit Framer Motion und Tailwind ein ansprechendes UI geschaffen. Der kreative Ansatz mit Sound-Effekten zeigt Liebe zum Detail.",
        "creativity": "Visuell charmant — die Animationen sind ein Hingucker. Die Idee ist da, die Umsetzung braucht mehr Zeit.",
        "improve": ["Macht die restlichen 23 Türchen funktionsfähig — extrahiert die Logik von Tag 5 als wiederverwendbare Komponente", "Splittet die App.tsx in Einzelkomponenten auf — ihr habt alles in einer Datei", "Schreibt ein README mit Setup-Anleitung und Feature-Beschreibung — das fehlt komplett"]
    },
    "ABB TS Chat Warriors": {
        "achievement": "Das ambitionierteste ABB-Projekt! Ein Echtzeit-WebSocket-System mit Server, mobilem Quiz und Grossbildschirm-Ansicht. Die 8 visuellen Themen mit eigener Canvas-Partikel-Engine (6 Partikeltypen!) sind beeindruckend. Alles von Hand in JavaScript gebaut — echte Handwerkskunst.",
        "creativity": "Höchste Kreativitätsbewertung! Die Canvas-Partikel-Engine und die 8 Themen zeigen genuines kreatives Talent. Das Echtzeit-Multi-Device-Konzept ist technisch anspruchsvoll und wurde komplett manuell umgesetzt.",
        "improve": ["Schreibt unbedingt ein README — euer Projekt ist das beeindruckendste der ABB-Teams, aber niemand weiss das ohne Dokumentation", "Migriert zu TypeScript — bei dieser Komplexität lohnt sich Typsicherheit", "Sichert die Admin-Endpoints mit einer einfachen Authentifizierung ab"]
    },
    "Autexis 01": {
        "achievement": "Drei funktionierende Applikationen in einem Turborepo-Monorepo! Das Convex-Backend mit 12+ Tabellen, das Next.js-Lieferantenportal und die Expo-Mobile-App sind beeindruckend. Die Dokumentation ist vorbildlich — 7 verlinkte Docs, Live-Demo, Video. Das ist Hackathon-Arbeit auf professionellem Niveau.",
        "creativity": "Der grösste Umfang aller Autexis-Teams. Die Convex-Serverless-Architektur ist eine clevere Wahl für Echtzeit-Features.",
        "improve": ["Ersetzt die Mock-Daten im Barcode-Scanner durch echte API-Aufrufe", "Fügt End-to-End-Tests für den kritischen Scan-Flow hinzu", "Der Lieferanten-Chat könnte mit KI-Zusammenfassungen erweitert werden"]
    },
    "Autexis 02": {
        "achievement": "Ihr habt ein durchdachtes Datenbankschema mit 8 Tabellen entworfen und das UI-Design als HTML-Mockups visualisiert. Das zeigt, dass ihr den Problemraum verstanden und strukturiert angegangen seid. Die SQL-Testdaten zeigen Verständnis für die Domäne.",
        "creativity": "Der Ansatz, zuerst die Datenstruktur zu planen, ist methodisch richtig. Die nächsten Schritte sind klar.",
        "improve": ["Baut ein einfaches Backend (Flask/Express) das eure SQL-Tabellen als API exponiert", "Verbindet die HTML-Seiten mit dem Backend — Formulare absenden, Daten laden", "Hasht die Passwörter — nie Klartext in der DB speichern (bcrypt oder argon2)"]
    },
    "Autexis 03": {
        "achievement": "Die beste Code-Qualität im gesamten Wettbewerb! Euer Go-Backend ist ein Lehrbuchbeispiel für Clean Architecture: saubere Schichtentrennung, Interface-getriebenes Design, Advisory Locks für Nebenläufigkeit, und 21 Testdateien mit Table-Driven Tests. Die Trust-Score-Engine mit 5 gewichteten Sub-Scores und Z-Score-Anomalieerkennung ist technisch brillant.",
        "creativity": "Exemplarische Engineering-Disziplin. Der Trust-Score-Algorithmus mit Advisory Locks zeigt tiefes technisches Verständnis.",
        "improve": ["Baut ein einfaches Frontend (React oder Vue) — die API ist bereit, es fehlt nur die Visualisierung", "Eine interaktive Trust-Score-Anzeige mit Aufschlüsselung wäre ein Wow-Effekt", "Deployed das Backend (z.B. auf Railway oder Fly.io) mit einer Demo-URL"]
    },
    "Autexis 04": {
        "achievement": "Enterprise-Architektur trifft Hackathon — und gewinnt. .NET Clean Architecture mit CQRS/Mediator, Keycloak OIDC mit echten Rollen, EPCIS 2.0 Standard-Compliance, und eine filmreife MapLibre-3D-Karte mit animierten Kamerafahrten. Dazu 5 KI-Fähigkeiten via Semantic Kernel. Das visuell eindrücklichste Frontend des Wettbewerbs.",
        "creativity": "Die Verbindung von Enterprise-Patterns mit einer cinematischen 3D-Visualisierung ist einzigartig. Zeigt sowohl Architektur-Verständnis als auch kreatives UI-Denken.",
        "improve": ["Verbindet die Mobile-App mit dem echten Backend statt Mock-Daten", "Fügt Integrationstests für den CQRS-Pipeline hinzu", "Eine Live-Demo-URL mit Beispieldaten würde den Wow-Faktor noch steigern"]
    },
    "Autexis 05": {
        "achievement": "Die strengste Engineering-Disziplin im Wettbewerb! 5-Workspace-Monorepo mit geteiltem typisierten SDK, MCP-Server und LangGraph-ReAct-KI-Agent. ESLint no-any-Regel, Stylelint, Husky Pre-Commit-Hooks, 27 Testdateien und Cypress E2E. RecallSwiss-Integration für echte Regierungsdaten. Das ist professionelle Softwareentwicklung.",
        "creativity": "Die MCP+LangGraph-Architektur ist kreativ und zukunftsweisend. Die geteilte SDK zwischen Frontend und MCP ist eine elegante Lösung.",
        "improve": ["Poliert das UI — die Funktionalität ist stark, das visuelle Design könnte mehr Liebe vertragen", "Fügt eine Demo-Tour für neue Benutzer hinzu", "Die RecallSwiss-Daten könnten visuell auf einer Karte dargestellt werden"]
    },
    "Autexis 06": {
        "achievement": "Tomapo hat eines der schönsten App-Designs im gesamten Wettbewerb! Die warme Farbpalette, das durchdachte UX-Design mit Supply-Chain-Tracing, CO2-Tracking, Qualitätsmeldungen und Community-Feed zeigen echtes Produktdenken. Die App sieht aus wie ein fertiges Produkt.",
        "creativity": "Visuell herausragend. Das UX-Design mit der warmen Erde/Grün-Farbpalette ist professionell und einladend.",
        "improve": ["Stellt den Code auf GitHub — ohne Repository kann die technische Leistung nicht bewertet werden", "Fügt eine README mit Tech-Stack und Setup-Anleitung hinzu", "Eine Live-Demo oder TestFlight-Link würde die Bewertung massiv verbessern"]
    },
    "Autexis 07": {
        "achievement": "Ihr habt ein funktionierendes Full-Stack-System mit Spring Boot Backend und Kotlin Android App gebaut. Das Backend hat eine saubere Service/Repository/Controller-Schichtung mit Batch-Tracking, Risk-Scoring basierend auf Community-Beschwerden, und OpenAPI-Dokumentation. Docker Compose mit PostgreSQL Healthcheck zeigt DevOps-Verständnis.",
        "creativity": "Das Risk-Scoring mit Community-Warning-Thresholds ist ein cleverer Ansatz für Crowdsourced Food Safety.",
        "improve": ["Erweitert die Android-App über die drei Standard-Fragments hinaus — sie ist noch sehr dünn", "Fügt ein README hinzu — es fehlt komplett", "Implementiert echte Authentifizierung — aktuell ist alles offen (CSRF disabled, permitAll)"]
    },
    "Autexis 08": {
        "achievement": "Das breiteste Feature-Set aller Autexis-Teams! 30+ API-Endpoints, 36 React-Komponenten, QR-Codes, KI-Chat, OCR-Scanning, Cold-Chain-Monitoring, Gamification mit Achievements und Leaderboard. Die i18n-Unterstützung und das 1650-Zeilen Custom-CSS zeigen Ambition. Ihr habt versucht, alles zu bauen — und erstaunlich viel davon funktioniert.",
        "creativity": "Beeindruckende Breite. Die Kombination aus Gamification, Cold-Chain und KI-Chat ist ambitioniert.",
        "improve": ["Schreibt ein README — das Projekt hat so viele Features, aber niemand weiss davon ohne Dokumentation", "Refactored die 1317-Zeilen router.py in mehrere Module", "Fügt Tests hinzu — bei 30+ Endpoints ist das kritisch für die Stabilität"]
    },
    "Autexis 09": {
        "achievement": "Eine polierte React Native App mit dem besten Atomic-Design-Komponentensystem (Atoms/Molecules/Organisms) im Wettbewerb. Die Gemini 2.5 Flash Integration für multimodales OCR mit Google Search Grounding für Echtzeit-Rückruferkennung ist technisch clever. Die README ist die beste unter den Autexis 08-10 Teams — mit Mermaid-Diagrammen und Sicherheitsüberlegungen.",
        "creativity": "Das Atomic-Design-Pattern und die Google Search Grounding für Live-Recalls sind kreative Architekturentscheidungen.",
        "improve": ["Baut ein Backend — die Mock-Daten limitieren die Demo erheblich", "Ein Server mit echten Produktdaten (z.B. von Open Food Facts) würde den Wow-Faktor steigern", "Fügt eine Offline-Funktionalität hinzu — als Mobile-App ist das ein wichtiges Feature"]
    },
    "Autexis 10": {
        "achievement": "Perfekte Bewertung — 30/30! Eine produktionsreif deployte Dual-Surface-App mit WASM-Barcode-Scanner, animiertem Mapbox-3D-Globus mit Flow-Partikeln, Drizzle ORM mit 11-Tabellen-Schema, Cerebras KI-Streaming, und Push-Notifications. 13 Vitest-Tests mit 90% Coverage-Schwelle, GitHub Actions CI/CD, Multi-Stage Docker. Herausragende Dokumentation mit Live-Demo, Figma, GIF-Walkthroughs und ADRs.",
        "creativity": "Die Origin-Intelligence-Engine mit FAO/USDA-Handelsdaten und das Batch-Lineage mit Union-Find-Clustering sind hochkreative Lösungen. Meisterhafte KI-Orchestrierung.",
        "improve": ["Ihr seid am Limit — eventuell Performance-Optimierung für den 3D-Globus auf schwächeren Geräten", "A/B-Testing der Consumer-PWA mit echten Nutzern wäre der nächste Schritt", "Die Push-Notification-Infrastruktur könnte mit echten Recall-Daten getestet werden"]
    },
    "BNAO 1": {
        "achievement": "Ihr habt eine saubere Website mit Vite und TailwindCSS gebaut und einen funktionierenden KI-Chatbot integriert. Das Dark-Mode-Support und das responsive Mobile-Menü zeigen Aufmerksamkeit für UX-Details. Die README erklärt den Tech-Stack und die CI/CD-Pipeline klar.",
        "creativity": "Der KI-Chatbot ist ein nützliches Feature für ein Bildungsnetzwerk. Die Pivot-Entscheidung vom CMS zeigt Pragmatismus.",
        "improve": ["Ersetzt die Template-Platzhalter (Your Company, Marketplace) durch echte Inhalte", "Macht die Navigation funktional — aktuell führen die meisten Links ins Leere", "Extrahiert den Header/Footer in wiederverwendbare Komponenten statt Copy-Paste"]
    },
    "BSI 1": {
        "achievement": "Eine polierte Event-Networking-App mit QR-Code-Scanning, KI-Icebreakern (25+ Fallbacks!), Smart-Matching mit gewichtetem Scoring, Echtzeit-Subscriptions, Session-Engagement mit Knowledge-Card-Tiers und CRM-Export. Sauberes TypeScript, Zod-Validierung und 7 Testdateien. Die README ist vorbildlich.",
        "creativity": "Das Session-Engagement-System mit Knowledge-Card-Tiers und der Passport sind originelle Features für Event-Apps.",
        "improve": ["Fügt End-to-End-Tests für den QR-Scan-zu-Verbindung-Flow hinzu", "Die Matching-Algorithmus-Gewichte könnten konfigurierbar gemacht werden", "Eine Projektoransicht mit grösserer Schrift für Konferenzräume wäre nützlich"]
    },
    "BSI 2": {
        "achievement": "Technisch herausragend! 24 von 26 Features funktionieren: WebRTC-Videoanrufe mit Timer, WebSocket-Chat, Echtzeit-Matching mit pessimistischer DB-Sperrung, Session-Check-in mit QR, Live-Reactions, Q&A mit Upvoting, Booth-System, Anti-Belästigungs-Features. 98 Testdateien und 18 Broadcast-Events. Service-orientierte Architektur mit Custom-Exception-Hierarchie.",
        "creativity": "Die Feature-Selektion (Serendipity Wave, Anti-Harassment, WebRTC mit Timer-Extension) zeigt kreatives Produktdenken. Der Matching-Algorithmus mit Interest-Overlap + Context-Match + Session-Affinity ist durchdacht.",
        "improve": ["Fügt Service-Worker für Offline-PWA-Support hinzu", "Die WebRTC-Verbindung könnte mit TURN-Servern für schwierige Netzwerke robuster werden", "Ein Onboarding-Tutorial würde neuen Nutzern den Einstieg erleichtern"]
    },
    "Data Unit 1": {
        "achievement": "Ihr habt einen funktionierenden Prototyp mit YOLO-Klassifikation, WebSocket-Echtzeit-Updates und automatischer Nachbestellung via Gmail und ERP-Webhook gebaut. Die Dokumentation mit Architektur-Diagramm und API-Tabelle ist gut. Das Presence-Tracking mit Miss/Confirm-Logik zeigt Domänenverständnis.",
        "creativity": "Der originale YOLO-Ansatz für Bestandsüberwachung mit automatischer Bestellung ist eine kreative Lösung für das KMU-Problem.",
        "improve": ["Fügt eine Datenbank hinzu — localStorage reicht nicht für Produktion", "Modularisiert die 793-Zeilen app.js in Einzelkomponenten", "Fügt Authentifizierung hinzu — das System bestellt automatisch, das braucht Zugangskontrolle"]
    },
    "Data Unit 3": {
        "achievement": "Das technisch reifste Data-Unit-Projekt! Ein eigenes PyTorch-Füllstand-Regressionsmodell mit In-App-Retraining, Verdeckungserkennung, aufeinanderfolgender Lesebestätigung vor Bestellung, Docker, SQLAlchemy ORM mit Migrationen, APScheduler, JWT-Auth und eine separate Docusaurus-Dokumentationsseite. Das zeigt tiefes ML-Verständnis.",
        "creativity": "Die In-App-Retraining-Fähigkeit ist einzigartig im Wettbewerb. Die Consecutive-Reading-Confirmation zeigt Verständnis für reale Betriebsbedingungen.",
        "improve": ["Optimiert die Trainingszeit — aktuell könnte das bei grossen Datasets langsam werden", "Fügt eine visuelle Trainingsfortschritts-Anzeige hinzu", "Ein A/B-Vergleich zwischen eurem Modell und einem vortrainierten Baseline wäre interessant"]
    },
    "Data Unit 4": {
        "achievement": "Der kreativste KI-Ansatz! Statt ein Modell zu trainieren, nutzt ihr Google Gemini mit natürlichsprachlichen Bestandsregeln — ein Zero-Training-Ansatz, der flexibler und schneller zu deployen ist. Das QR-Kamera-Pairing, die Insights-Debugging-Konsole und das Mobile-First-Design zeigen starkes Produktdenken. Die polierteste UI aller Data-Unit-Teams.",
        "creativity": "Der Zero-Training-Gemini-Ansatz ist eine genuinely kreative Architekturentscheidung. Die Insights-Console und QR-Pairing sind originelle UX-Ideen.",
        "improve": ["Fügt Caching für Gemini-Antworten hinzu — wiederholte Scans desselben Produkts sollten schneller sein", "Ein Confidence-Threshold für automatische vs. manuelle Bestellung wäre sinnvoll", "Vergleicht die Genauigkeit mit einem klassischen CV-Ansatz für die Dokumentation"]
    },
    "Data Unit 6": {
        "achievement": "Wo Software auf Hardware trifft! Eine Multi-Modell-KI-Pipeline (Grounding DINO, Florence-2, SAM2, eigenes YOLO26n) die intelligent das beste Modell pro Produkttyp wählt. Raspberry Pi Edge-Device mit PiCamera2, Supabase PostgreSQL, SendGrid-E-Mails, Vue.js-Dashboard, GitHub Actions CI/CD via Tailscale VPN. 31-Test-Pytest-Suite und die umfassendste Dokumentation (868 Zeilen) mit Security-Audit.",
        "creativity": "Die intelligente Modell-Auswahl pro Produkttyp und die Hardware-Integration zeigen tiefes ML-Verständnis und echtes Systems-Engineering. Höchste Kreativitätsbewertung!",
        "improve": ["Fügt ein automatisches Failover hinzu wenn ein Modell keine guten Ergebnisse liefert", "Eine Kalibrierungs-UI für neue Produkt-Zonen würde die Einrichtung vereinfachen", "Testet mit verschiedenen Lichtverhältnissen — das ist der grösste Praxisfaktor"]
    },
    "Kromer 1": {
        "achievement": "Ein gut strukturierter 9-seitiger Forschungsbericht zu IT-Security-Tools für Kromer Print AG. Der Vergleich von Greenbone/OpenVAS, Tenable Nessus und Rapid7 InsightVM mit Kostenanalyse in CHF und praktischen Tests auf Azure VMs zeigt methodisches Vorgehen. Die klare Empfehlung (OpenVAS + Wazuh) ist fundiert und praxisbezogen.",
        "creativity": "Methodisch solide Analyse mit echten Tests. Die Kostenvergleiche sind für ein KMU direkt umsetzbar.",
        "improve": ["Baut einen kleinen Prototyp — z.B. ein Dashboard das Wazuh-Daten visualisiert", "Automatisiert den Vergleich als Script das auf verschiedenen Umgebungen läuft", "Eine Live-Demo der Wazuh-Installation mit echten Scan-Ergebnissen wäre überzeugend"]
    },
    "zB 1": {
        "achievement": "Die stärkste Einzelleistung ohne KI-Tools! Ein produktionsreifes Klassenzimmer-Internet-Management mit dualen Firewall-Backends (Dry-Run + nftables), dualen Webfilter-Backends (Dry-Run + Squid mit TLS-Interception), wiederverwendbaren Whitelist-Templates, Per-Weekday-Schedules mit Manual Override, DNS-Refresh-Scheduler, Rate-Limiting, Audit-Logging, Alembic-Migrationen und umfassender Test-Suite. 4'788 Zeilen reines Python — alles von Hand.",
        "creativity": "Höchste Kreativitätsbewertung! Das abstrakte Backend-Pattern mit Dry-Run- und Produktions-Implementierungen zeigt echtes Architektur-Denken. Ohne jegliche KI-Unterstützung — die authentischste Engineering-Leistung.",
        "improve": ["Baut ein modernes SPA-Frontend (Vue oder React) — das ist die einzige echte Schwäche", "Eine visuelle Darstellung der aktiven Firewall-Regeln wäre für Lehrer hilfreich", "Fügt WebSocket-Notifications hinzu wenn Regeln sich ändern"]
    },
    "zB 2": {
        "achievement": "Ein funktionierendes VLAN-Whitelist-Management mit CSRF-Schutz, Rate-Limiting, bcrypt-Hashing und nftables-Config-Generierung mit DNS-Auflösung. Die Dokumentation ist beeindruckend — 900+ Zeilen mit ASCII-Diagrammen für Datenflüsse, ER-Diagramm, Middleware-Execution-Order und Skalierungsstrategie. Ihr versteht nicht nur was ihr baut, sondern könnt es auch erklären.",
        "creativity": "Die Dokumentationstiefe ist bemerkenswert. Die Sicherheitsfeatures (CSRF, Rate-Limiting, Session-Hardening) zeigen Bewusstsein für Production-Readiness.",
        "improve": ["Entfernt die committed node_modules und .env mit Credentials aus dem Repo — das ist ein Sicherheitsrisiko", "Fügt automatisierte Tests hinzu — die Dokumentation beschreibt was getestet werden sollte", "Ersetzt die deprecated csurf-Package durch eine moderne Alternative"]
    },
    "zB 3": {
        "achievement": "Der kühnste technische Ansatz im gesamten Wettbewerb! Während alle anderen Web-Apps bauten, habt ihr ein echtes MITM-Proxy-basiertes Netzwerk-Filtersystem mit mitmproxy, CoreDNS und Per-Klassenzimmer-Docker-Containern gebaut. Die Integration von SvelteKit-Frontend, Node.js-Backend, MySQL und CoreDNS in einer Docker-Compose-Orchestrierung zeigt echtes Systems-Engineering. Höchste Kreativitätsbewertung!",
        "creativity": "Der genuinely neuartigste Ansatz. Ein echtes Proxy-System statt einer Web-App — das erfordert Netzwerk-Wissen das man nicht faken kann.",
        "improve": ["Fixt die SQL-Injection im mitmproxy-Addon — verwendet parametrisierte Queries statt String-Formatting", "Investiert in das UI — die Funktionalität ist stark aber die Oberfläche ist noch roh", "Fügt Logging/Monitoring hinzu damit Lehrer sehen können welche Seiten blockiert werden"]
    },
    "zB 4": {
        "achievement": "Die polierteste UI aller zB-Teams! Vue 3 + TypeScript SPA mit TailwindCSS, responsive Room-Cards, Toast-Notifications, Bestätigungsdialoge, Schedule-Modal und Bulk-Toggle. LDAP-Auth, Firewall-Agent mit Mock/Shorewall-Driver-Pattern, Audit-Logging, Docker Compose mit LDAP Healthcheck. Dazu pytest + vitest Test-Suites und GitHub Actions CI.",
        "creativity": "Das Driver-Pattern für den Firewall-Agent (Mock/Shorewall) ist eine clevere Abstraktion. Die UI-Politur und Test-Abdeckung zeigen professionelle Arbeitsweise.",
        "improve": ["Fügt Alembic/Migrationen hinzu — aktuell fehlt ein DB-Migrations-System", "Macht die CORS-Konfiguration restriktiver — aktuell ist es ein Wildcard", "Eine Echtzeit-Statusanzeige der Firewall-Regeln wäre für Admins nützlich"]
    },
    "Data Unit 2": {
        "achievement": "Leider konnten wir euer Projekt nicht bewerten — der SharePoint-Link war nicht zugänglich. Stellt sicher, dass euer Code auf einer öffentlich zugänglichen Plattform wie GitHub liegt.",
        "creativity": "Nicht bewertbar.",
        "improve": ["Erstellt ein GitHub-Repository für euer Projekt", "Fügt eine README mit Setup-Anleitung hinzu", "Stellt sicher dass der Link vor der Deadline funktioniert"]
    },
    "Data Unit 5": {
        "achievement": "Leider konnten wir euer Projekt nicht bewerten — der GitHub-Link (github.com/baden-hackt) führt zu einer 404-Seite. Prüft ob die URL korrekt ist oder ob das Repository privat ist.",
        "creativity": "Nicht bewertbar.",
        "improve": ["Prüft die Repository-URL und macht es öffentlich", "Fügt eine README mit Projektbeschreibung hinzu", "Ein funktionierender Demo-Link hilft der Jury enorm"]
    },
    "BSI 3": {
        "achievement": "Leider konnten wir euer Projekt nicht bewerten — es wurde kein Einreichungslink gefunden.",
        "creativity": "Nicht bewertbar.",
        "improve": ["Stellt euren Code auf GitHub oder einer ähnlichen Plattform bereit", "Dokumentiert was ihr gebaut habt, auch wenn es nicht fertig ist", "Jeder Fortschritt zählt — zeigt was ihr geschafft habt"]
    },
}


@app.route("/teams")
def teams_page():
    return render_template("teams.html", all_teams=get_all_teams(), feedback=TEAM_FEEDBACK,
                           tech_eval=TECH_EVAL)


@app.route("/teams/<path:team_name>")
def team_detail(team_name):
    if team_name not in TEAM_FEEDBACK:
        return "Team nicht gefunden", 404
    return render_template("teams.html", all_teams=get_all_teams(), feedback=TEAM_FEEDBACK,
                           tech_eval=TECH_EVAL, selected_team=team_name)


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
    # Load jury factors
    conn2 = get_db()
    factors = {r["team_name"]: r["jury_factor"] for r in conn2.execute("SELECT team_name, jury_factor FROM teams").fetchall()}
    conn2.close()

    for team in active:
        jf = factors.get(team, 1.0) or 1.0
        entry = {"team": team, "avg_total": 0, "num_judges": 0, "criteria_avg": {}, "jury_factor": jf, "weighted_total": 0}
        if team in team_scores:
            ts = team_scores[team]
            avg = round(sum(ts["totals"]) / len(ts["totals"]), 1)
            entry["avg_total"] = avg
            entry["weighted_total"] = round(avg * jf, 1)
            entry["num_judges"] = len(ts["totals"])
            for c_key, _ in CRITERIA:
                vals = ts["criteria"][c_key]
                entry["criteria_avg"][c_key] = round(sum(vals) / len(vals), 1) if vals else 0
        leaderboard.append(entry)

    leaderboard.sort(key=lambda x: -x["weighted_total"])
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
        conn.execute("INSERT OR REPLACE INTO teams (team_name, active, sort_order, jury_factor) VALUES (?, ?, ?, ?)",
                     (t["team_name"], t.get("active", 0), t.get("sort_order", 0), t.get("jury_factor", 1.0)))
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


@app.route("/api/jury-factors", methods=["GET"])
def get_jury_factors():
    conn = get_db()
    rows = conn.execute("SELECT team_name, jury_factor FROM teams WHERE active=1 ORDER BY sort_order, team_name").fetchall()
    conn.close()
    return jsonify([{"team_name": r["team_name"], "jury_factor": r["jury_factor"] or 1.0} for r in rows])


@app.route("/api/jury-factors", methods=["POST"])
def set_jury_factors():
    data = request.json
    factors = data.get("factors", {})
    conn = get_db()
    for team_name, factor in factors.items():
        f = max(0.0, min(2.0, float(factor)))
        conn.execute("UPDATE teams SET jury_factor=? WHERE team_name=?", (f, team_name))
    conn.commit()
    conn.close()
    return jsonify({"status": "saved", "count": len(factors)})


init_db()
restore_from_local_backup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
