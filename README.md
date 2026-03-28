# Hackathon Jury App

A mobile-first web app for hackathon jury evaluation, built with Flask and deployed to SAP BTP Cloud Foundry.

## Features

- **PIN-protected access** with numpad login
- **Judge selection** — tap your name, start scoring
- **Team tiles** — alphabetical grid, green checkmark when scored
- **5 criteria scoring** (1-4 scale) with comments
- **Live dashboard** — auto-refreshing leaderboard for big-screen projection
- **Admin panel** — CRUD for teams and judges, team activation toggles
- **Versioning** — archive rounds without losing data
- **Excel export** — download all scores
- **QR code sharing** — share the app URL via QR
- **Tech evaluation integration** — show AI-generated code review summaries per team
- **Deploy-safe** — backup/restore API preserves data across redeployments

## Setup

1. Copy `config.example.json` to `config.json` and customize:
   - Set your PIN, judges, teams, and optionally tech evaluation data
   - Teams with `"active": 1` are shown to judges for scoring

2. Install dependencies:
   ```bash
   pip install flask openpyxl gunicorn
   ```

3. Run locally:
   ```bash
   python app.py
   ```

4. Open `http://localhost:8080`

## Deploy to SAP BTP Cloud Foundry

```bash
cf login -a <your-api-endpoint>
./deploy.sh
```

The `deploy.sh` script automatically backs up data before push and restores after.

## Configuration

All event-specific data lives in `config.json` (gitignored):

| Field | Description |
|-------|-------------|
| `pin` | Access PIN for the app |
| `app_url` | Public URL (for QR code generation) |
| `judges` | List of judge names |
| `teams` | List of `{name, active}` objects |
| `tech_eval` | Optional: per-team technical evaluation summaries |

## Tech Stack

- **Backend:** Python Flask + SQLite
- **Frontend:** Single HTML template + Tailwind CSS (CDN)
- **Deployment:** SAP BTP Cloud Foundry (Python buildpack)
