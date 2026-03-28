#!/bin/bash
# Deploy script that preserves all data across cf push
set -e

APP_URL="https://badenhackt-judge.cfapps.us10-001.hana.ondemand.com"
BACKUP_FILE="backup_state.json"

echo "=== Baden Hackt Judge App Deploy ==="

# Step 1: Backup current state (if app is running)
echo ">> Backing up current state..."
HTTP_CODE=$(curl -s -o "$BACKUP_FILE" -w "%{http_code}" "$APP_URL/api/backup" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    TEAMS=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['teams']))")
    JUDGES=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['judges']))")
    SCORES=$(python3 -c "import json; d=json.load(open('$BACKUP_FILE')); print(len(d['scores']))")
    echo "   Backed up: $TEAMS teams, $JUDGES judges, $SCORES scores"
else
    echo "   No running app found (first deploy or app down). Skipping backup."
    rm -f "$BACKUP_FILE"
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

# Step 5: Restore state
if [ -f "$BACKUP_FILE" ]; then
    echo ">> Restoring state..."
    RESTORE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$APP_URL/api/restore" \
        -H "Content-Type: application/json" \
        -d @"$BACKUP_FILE")
    if [ "$RESTORE_CODE" = "200" ]; then
        echo "   State restored successfully!"
    else
        echo "   WARNING: Restore failed (HTTP $RESTORE_CODE). Backup saved in $BACKUP_FILE"
    fi
else
    echo ">> No backup to restore (fresh deploy)."
fi

echo ""
echo "=== Deploy complete ==="
echo "App URL: $APP_URL"
echo "Admin:   $APP_URL/admin"
echo "Dashboard: $APP_URL/dashboard"
