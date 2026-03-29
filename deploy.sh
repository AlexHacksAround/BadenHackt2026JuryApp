#!/bin/bash
# Deploy script that preserves all data across cf push
set -e

APP_URL="https://badenhackt-judge.cfapps.us10-001.hana.ondemand.com"
BACKUP_DIR="backups"
mkdir -p "$BACKUP_DIR"

echo "=== Baden Hackt Judge App Deploy ==="

# Step 1: Backup current state with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/backup_${TIMESTAMP}.json"

echo ">> Backing up current state..."
HTTP_CODE=$(curl -s -o "$BACKUP_FILE" -w "%{http_code}" "$APP_URL/api/backup" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    SCORES=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['scores']))")
    TEAMS=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['teams']))")
    JUDGES=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['judges']))")
    echo "   Saved: $BACKUP_FILE ($TEAMS teams, $JUDGES judges, $SCORES scores)"
else
    echo "   App not reachable. No new backup."
    rm -f "$BACKUP_FILE"
fi

# Find best backup (most scores)
BEST_BACKUP=$(python3 -c "
import json, glob, os
best, best_n = None, -1
for f in sorted(glob.glob('${BACKUP_DIR}/backup_*.json')):
    try:
        n = len(json.load(open(f))['scores'])
        if n >= best_n:
            best, best_n = f, n
    except: pass
if best: print(best)
")
if [ -n "$BEST_BACKUP" ]; then
    BEST_SCORES=$(python3 -c "import json; print(len(json.load(open('$BEST_BACKUP'))['scores']))")
    echo "   Best backup: $BEST_BACKUP ($BEST_SCORES scores)"
fi

# Step 2: Clean local artifacts
rm -f scores.db

# Step 3: Push
echo ">> Deploying to Cloud Foundry..."
cf push badenhackt-judge 2>&1 | tail -5

# Step 4: Wait for app to be ready
echo ">> Waiting for app to start..."
sleep 5
for i in $(seq 1 10); do
    if curl -sf "$APP_URL/" > /dev/null 2>&1; then
        echo "   App is ready!"
        break
    fi
    echo "   Attempt $i/10..."
    sleep 3
done

# Step 5: Restore from best backup
if [ -n "$BEST_BACKUP" ]; then
    echo ">> Restoring from: $BEST_BACKUP"
    RESTORE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$APP_URL/api/restore" \
        -H "Content-Type: application/json" \
        -d @"$BEST_BACKUP")
    if [ "$RESTORE_CODE" = "200" ]; then
        echo "   State restored successfully!"
    else
        echo "   WARNING: Restore failed (HTTP $RESTORE_CODE)"
    fi
else
    echo ">> No backup to restore."
fi

echo ""
echo "=== Deploy complete ==="
echo "App URL: $APP_URL"
echo "Backups: $(ls ${BACKUP_DIR}/backup_*.json 2>/dev/null | wc -l | tr -d ' ') versions in ${BACKUP_DIR}/"
