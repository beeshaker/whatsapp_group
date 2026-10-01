# Pixiilive: Hard Reset and Rebuild from WhatsApp History

One-time procedure. It does four things:

1. Fixes Pixiilive's disconnects by moving it to the Baileys engine.
2. Wipes all of Pixiilive's existing tickets.
3. Re-links the bot and asks the phone for its full chat history.
4. Rebuilds the tickets from that history.

**Time needed:** about 30 minutes hands-on, plus the rebuild in step 9, which runs in the background and can take several hours.

**You will need:**
- SSH access: `ssh deploy@167.86.81.124`
- **The Pixiilive bot phone in hand** for the QR scan in step 7
- A quiet time to do it. The bot is offline from step 4 until the scan in step 7, usually a few minutes.

> **Read this first.** Step 4 permanently deletes every Pixiilive ticket, status, edit and reply history. Rebuilt tickets all start as `review`. The backup in step 3 is the only way back. Do not skip it.

---

## Step 0: Get the code onto the server (from your machine)

The work is on the `fix/whatsapp-reconnect` branch (4 commits, including this runbook). Merge it and push:

```bash
cd ~/projects/whatsappgroup/whatsapp-ticketing
git switch master
git merge fix/whatsapp-reconnect
git push origin master
```

---

## Step 1: Pull the code on the server

```bash
ssh deploy@167.86.81.124
cd /opt/whatsapp-ticketing
git pull
git log --oneline -4
```

✅ **Check:** the list includes `feat: hard reset + full-history rebuild ...`, `feat: capture Baileys link-time history ...` and `fix: make WhatsApp reconnect actually recover ...`. Also `ls docs/pixiilive-reset-runbook.md` must find this file. If it doesn't, stop. The next steps would deploy old code without any error.

---

## Step 2: Deploy the new code to Pixiilive

**OpenWA** (the WhatsApp gateway):

```bash
rsync -a --delete --exclude='.env' --exclude='data' \
  /opt/whatsapp-ticketing/openwa/ /opt/clients/pixiilive/openwa/
cd /opt/clients/pixiilive
docker compose build openwa
docker compose up -d --no-deps openwa
```

**Backend** (dashboard and ticket pipeline):

```bash
cd /opt/whatsapp-ticketing
./deploy/scripts/update-clients.sh pixiilive
```

✅ **Check:** both containers are up and the scripts exist.

```bash
cd /opt/clients/pixiilive
docker compose ps                                   # backend and openwa both "Up"
docker compose exec backend ls scripts/             # shows reset_tickets.py and backfill_history.py
```

---

## Step 3: Back up the database

```bash
cd /opt/clients/shared-postgres
docker compose exec postgres psql -U ops_user -d postgres -c '\l' | grep pixii   # confirm the DB name
docker compose exec -T postgres pg_dump -U ops_user --clean --if-exists client_pixiilive \
  > ~/client_pixiilive-before-reset-$(date +%F).sql
ls -lh ~/client_pixiilive-before-reset-*.sql
```

✅ **Check:** the `.sql` file exists and is not tiny (a few KB would mean the dump failed). If the database name from `\l` differs, use that name instead.

---

## Step 4: Wipe the tickets

This has to happen **before** the re-link. Once the bot is linked, new messages come in live, and those are not part of the history sync. Wiping after the link would lose them for good.

```bash
cd /opt/clients/pixiilive
docker compose exec backend python scripts/reset_tickets.py
```

This is a dry run. It prints how many tickets, updates, media files and audit rows exist. Note the numbers, then:

```bash
docker compose exec backend python scripts/reset_tickets.py --apply --confirm pixiilive
```

✅ **Check:** it prints `Deleted all ticket rows and N media files.` The Pixiilive dashboard now shows no tickets. Users and group settings are still there.

---

## Step 5: Switch to Baileys with full history

```bash
docker run --rm -v pixiilive_openwa_data:/data alpine sh -c \
  "echo 'ENGINE_TYPE=baileys' >> /data/.env.generated && echo 'BAILEYS_FULL_HISTORY=true' >> /data/.env.generated"
docker run --rm -v pixiilive_openwa_data:/data alpine cat /data/.env.generated
```

✅ **Check:** the file shows both `ENGINE_TYPE=baileys` and `BAILEYS_FULL_HISTORY=true`, each **once**. If either appears twice (or an old `ENGINE_TYPE` line exists), edit the file so each appears once:

```bash
docker run --rm -it -v pixiilive_openwa_data:/data alpine vi /data/.env.generated
```

Then recreate the container so it picks the settings up:

```bash
cd /opt/clients/pixiilive
docker compose up -d --force-recreate openwa
docker compose logs openwa | grep -i "engine plugin enabled"     # should say: baileys
```

---

## Step 6: Start watching the logs

Open a **second** SSH window and leave this running:

```bash
cd /opt/clients/pixiilive
docker compose logs -f openwa | grep -E "History sync|Session ready|QR code generated|disconnected"
```

---

## Step 7: Re-link with the bot phone

1. Open `https://pixiilive.whats2manage.com/settings`, signed in as an admin.
2. Click **Link with new QR** and confirm.
3. On the Pixiilive bot phone, open **WhatsApp → ⋮ → Linked Devices**.
   - Remove any old linked device for this bot.
   - Then tap **Link a Device** and scan the QR.
4. The page should turn green and say **Connected**.

✅ **Check:** in the second window you see `Session ready`, then several `History sync: N messages, M kept (...)` lines. **Wait until one line ends in `isLatest=true`.** For a busy phone, full history can take 5 to 30 minutes. Keep the phone online and on Wi-Fi until then.

If no QR appears or the page shows an error, see **Troubleshooting** below.

---

## Step 8: Dry-run the rebuild

```bash
cd /opt/clients/pixiilive
docker compose exec backend python scripts/backfill_history.py --from-start | tail -40
docker compose exec backend python scripts/backfill_history.py --from-start | grep eligible
```

✅ **Check:**
- `N messages in window, M eligible ticket-group messages`: M should be well above zero.
- The earliest dates in the list show how far back the phone's history goes. The phone decides this; it is often months.
- Only real ticket groups appear (no billing group, no DMs).

If `M` is `0`, stop and check Troubleshooting. The rebuild would create nothing.

---

## Step 9: Run the rebuild (in the background)

It makes one AI classifier call per message, so it can take hours. Running it in the background means an SSH drop won't stop it.

```bash
cd /opt/clients/pixiilive
docker compose exec -d backend sh -c \
  "python scripts/backfill_history.py --from-start --apply > /app/media/backfill.log 2>&1"
docker compose exec backend tail -f /app/media/backfill.log
```

Each message prints a line followed by `-> staged`, `-> noise` or `-> duplicate`. You can close the window; check back later with the same `tail` command.

✅ **Check:** the last line reads `Done: {...}; N new tickets tagged action='history_backfill' in audit_log`, and the dashboard shows the rebuilt tickets with their original dates.

**If it stops partway** (container restart, server reboot), just run the step 9 command again. Messages already done are skipped.

---

## Step 10: Turn full-history sync off

If you leave it on, every future re-link downloads the whole history again.

```bash
docker run --rm -v pixiilive_openwa_data:/data alpine sed -i '/^BAILEYS_FULL_HISTORY=/d' /data/.env.generated
docker run --rm -v pixiilive_openwa_data:/data alpine cat /data/.env.generated   # only ENGINE_TYPE=baileys left
cd /opt/clients/pixiilive
docker compose up -d --force-recreate openwa
```

✅ **Check:** after about 30 seconds, Settings shows **Connected** again **without** a new QR scan. The login is kept, and the session restarts by itself on boot.

Do this only after step 9 shows `Done`. The rebuild reads the saved history from OpenWA, and recreating the container while it runs would interrupt that read.

---

## Step 11: Deploy billing (admin side)

The admin client page gets the new **Restart WhatsApp** / **Link with new QR** buttons.

```bash
cd /opt/whatsapp-ticketing
diff -q billing/main.py /opt/billing/main.py
cp billing/main.py billing/whatsapp.py /opt/billing/
cp billing/templates/client_detail.html /opt/billing/templates/
cd /opt/billing
docker compose build billing
docker compose up -d --no-deps billing
```

**Required right after.** Recreating billing drops its client network connections, and billing messages break for everyone until they're reconnected:

```bash
docker network connect nineonetwo_client-net billing-app
docker network connect pixie_client-net billing-app
docker network connect pixiilive_client-net billing-app
docker network ls --format '{{.Name}}' | grep _client-net     # reconnect any other client listed here too
```

✅ **Check:** open `https://whats2manage.com`, then the Pixiilive client. The badge says **WhatsApp connected**.

---

## Step 12: Watch for 24 to 48 hours

```bash
cd /opt/clients/pixiilive
docker compose logs --since 24h openwa | grep -Ei "disconnected|reconnect|failed|History sync"
```

**Healthy:** an occasional `disconnected` followed by `Session ready` within a minute. It now recovers by itself.

**Not healthy:**
- Repeated `Max reconnect attempts reached`.
- A `Session disconnected: ... (401)` line, which means WhatsApp unlinked the device. Settings will show **Scan the QR code**.

Use **Restart connection** first, then **Link with new QR** if needed. Either works from the tenant Settings page or the admin client page.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No QR after **Link with new QR**, page says "Connecting…" for over a minute | `docker compose logs --tail 50 openwa`. If it shows `Connection Failure` before any QR, the WA version fetch failed: `docker compose restart openwa`, then click **Link with new QR** again. |
| Page shows "This WhatsApp gateway is out of date" | Step 2 didn't deploy the new OpenWA. Redo step 1 (check the commit) and step 2. |
| QR scanned but never turns Connected | On the phone, remove the device under Linked Devices, then click **Link with new QR** and scan again. |
| No `History sync` lines after Connected | Check step 5: `BAILEYS_FULL_HISTORY=true` must be in `.env.generated` **before** the container was recreated. Fix it, recreate the container, then do step 7 again. History is only sent at link time. |
| Step 8 shows `0 eligible` | Make sure the ticket groups are still allowed: Settings → Ticket-Raising Groups. Then run `docker compose exec backend python scripts/backfill_history.py --from-start` and look at the first line: if `N messages in window` is 0, no history was captured (see the row above). |
| Something went badly wrong and you need the old tickets back | Restore the step 3 backup, which replaces the whole Pixiilive database with its state before step 4. Stop the backend first: `cd /opt/clients/pixiilive && docker compose stop backend`. Then: `cd /opt/clients/shared-postgres && docker compose exec -T postgres psql -U ops_user -d client_pixiilive < ~/client_pixiilive-before-reset-<date>.sql`. Then `cd /opt/clients/pixiilive && docker compose start backend`. Media files deleted in step 4 can't be restored this way. |

---

## Reference

- **Backfilled tickets:** every rebuilt ticket has an `audit_log` row with `action = 'history_backfill'`.
- **Saved history on the server:** `pixiilive_openwa_data` volume, `baileys/history/pixiilive.jsonl` (adjust the name if the session isn't called `pixiilive`).
- **Background:** `docs/vps-architecture.md`, sections "WhatsApp session recovery", "Backfilling messages missed during an outage" and "Hard reset".
