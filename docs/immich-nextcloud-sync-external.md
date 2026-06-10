# Immich + Nextcloud: Photo Sync via rsync, Published with Cloudflare Tunnel

Sync photos from a NAS (fronted by Nextcloud SMB External Storage) into Immich as an External Library, and publish Immich securely with Cloudflare Tunnel — no open ports, no Docker volume gymnastics.

**Stack:** Proxmox VE · Immich (native LXC install, [community-scripts](https://github.com/community-scripts/ProxmoxVE)) · Debian NAS with Samba · Nextcloud · Cloudflare Tunnel

---

## Why this design

- **Phones upload via the Nextcloud app**, not Immich — Nextcloud writes through SMB to the NAS, and Immich pulls a copy from the NAS on a timer. Each tool does what it's best at: Nextcloud handles sync/upload, Immich handles browsing, search, faces and memories.
- **Pull-based rsync without `--delete`** — the Immich copy doubles as an archive on separate hardware. A fat-fingered delete on a phone never destroys the only copy.
- **Photos live on plain NAS shares**, accessed by Nextcloud as External Storage. The sync therefore deals in ordinary files — no Nextcloud data-directory internals, no `files_trashbin`/`appdata` exclusions, no file-cache worries.
- **Cloudflare Tunnel** — TLS at the edge, zero inbound firewall rules. The free plan's 100 MB upload cap doesn't matter because nothing uploads through Immich.
- **Why not NFS/bind-mount the NAS into Immich?** It works, but the library goes offline whenever the NAS or network hiccups, and you lose the second-copy benefit. Local copy + hourly sync is more resilient. (Bind mounts also can't cross Proxmox nodes — a mount point on the container only works for paths local to the node it runs on.)

```
Phones (Nextcloud app)
      │  auto-upload
      ▼
Nextcloud ──SMB──► NAS
                    ├── /mnt/media/photos                      (main library)
                    └── /mnt/downloads/InstantUpload/Camera    (phone uploads)
                          │
                          │  rsync over SSH (hourly, pull)
                          ▼
                  Immich LXC
                    └── /mnt/storage/photos/{library,camera}
                          │  External Library (read-in-place)
                          ▼
                  Immich ◄── Cloudflare Tunnel ──► https://photos.example.com
```

## Prerequisites

- Immich running (this guide assumes the community-scripts **native** LXC install — if yours is Docker, you'll additionally need to map the photo directory into the `immich-server` container as a volume, since Immich validates import paths inside the container).
- A NAS reachable over SSH with the photos on local paths (here: `/mnt/media/photos` and `/mnt/downloads/InstantUpload/Camera`, owned by user `nasuser`).
- Enough local disk in the Immich container/VM for a full copy of the library.
- A domain on Cloudflare.

Placeholders used throughout: `NAS_IP`, `nasuser`, `photos.example.com` — substitute your own.

---

## 1. SSH key from Immich host to NAS

On the Immich host:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_nas_sync -N ""
ssh-copy-id -i /root/.ssh/id_nas_sync.pub nasuser@NAS_IP

# verify — should list your photo folders with no password prompt
ssh -i /root/.ssh/id_nas_sync nasuser@NAS_IP "ls /mnt/media/photos | head"
```

rsync must exist on **both** ends. Minimal LXC images often lack it:

```bash
apt update && apt install -y rsync
ssh -i /root/.ssh/id_nas_sync nasuser@NAS_IP "which rsync"   # check the NAS too
```

## 2. Sync script

`/usr/local/bin/nextcloud-photo-sync.sh`:

```bash
#!/bin/bash
KEY="/root/.ssh/id_nas_sync"
NAS="nasuser@NAS_IP"
DEST="/mnt/storage/photos"

mkdir -p "$DEST/library" "$DEST/camera"

# Main photo library (Nextcloud external storage)
rsync -a --chmod=D755,F644 -e "ssh -i $KEY" \
  "$NAS:/mnt/media/photos/" \
  "$DEST/library/"

# Phone auto-uploads (Nextcloud InstantUpload)
rsync -a --chmod=D755,F644 -e "ssh -i $KEY" \
  "$NAS:/mnt/downloads/InstantUpload/Camera/" \
  "$DEST/camera/"
```

```bash
chmod +x /usr/local/bin/nextcloud-photo-sync.sh
```

Notes:

- **`--chmod=D755,F644` is not optional.** The community-scripts install runs Immich as a dedicated `immich` user, and plain `rsync -a` faithfully preserves whatever permissions the NAS had. If even *one* directory in the tree isn't readable by that user, Immich's library crawl **aborts entirely with EACCES — you get 0 assets imported, not a partial import.** This flag forces directories to 755 and files to 644 on the destination. (Bitten by this in production; see Troubleshooting.)
- **Trailing slashes matter** with rsync — `src/` → `dest/` copies contents, not the folder.
- **No `--delete`** by design: deletions upstream never remove anything from the Immich copy. For a mirror with a safety net instead, use `--delete --backup --backup-dir=/mnt/storage/photo-deleted/$(date +%F)`.

### First run

The initial pull can take hours. Run it in tmux with progress:

```bash
tmux new -s photosync
rsync -a --chmod=D755,F644 --info=progress2 -e "ssh -i /root/.ssh/id_nas_sync" \
  nasuser@NAS_IP:/mnt/media/photos/ /mnt/storage/photos/library/
```

Detach with `Ctrl+B` then `D`. When it finishes, run the script once to pick up the camera folder (already-copied files are skipped).

## 3. Hourly systemd timer

`/etc/systemd/system/nextcloud-photo-sync.service`:

```ini
[Unit]
Description=Pull Nextcloud photos from NAS

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nextcloud-photo-sync.sh
```

`/etc/systemd/system/nextcloud-photo-sync.timer`:

```ini
[Unit]
Description=Hourly Nextcloud photo sync

[Timer]
OnCalendar=hourly
RandomizedDelaySec=5m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now nextcloud-photo-sync.timer
systemctl list-timers nextcloud-photo-sync.timer
```

Want faster pickup? `OnCalendar=*:0/15` for every 15 minutes.

## 4. Immich External Library

Admin → **External Libraries** → create a library (owner = your user) → add **two** import folders:

1. `/mnt/storage/photos/library`
2. `/mnt/storage/photos/camera`

> **ENOENT / "Path does not exist"?** Immich validates the path the moment you add it. Create the folders first (`mkdir -p`), or run the script once. On Docker installs, the path must also be volume-mapped into `immich-server`.

Leave the default exclusion patterns (`@eaDir`, `#recycle`, etc.) — they filter NAS junk and cost nothing.

Recommended settings:

| Setting | Value | Why |
|---|---|---|
| `library.watch.enabled` | `true` | inotify on a local filesystem means new photos appear seconds after each sync. |
| `library.scan.cronExpression` | e.g. `0 1 * * *` | Nightly safety-net scan — staggered away from Immich's nightly tasks (00:00) and DB backup. |
| `server.externalDomain` | `https://photos.example.com` | Must match your public URL exactly (no port) — it's used to build share links. |

External libraries are read-in-place: Immich never writes to or moves the files. Scanning while the first sync is still running is safe — the scan registers whatever is on disk and the next scan catches the rest.

## 5. Cloudflare Tunnel

1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel** (Cloudflared connector).
2. Install the connector on the Immich host using the Debian command Cloudflare shows you (adds the apt repo, registers a systemd service with the token baked in).
3. Public hostname: subdomain `photos`, your domain, service `http://localhost:2283`.
4. Save — Cloudflare creates the proxied CNAME. Test from mobile data, not just your LAN.

Skip Cloudflare Access in front of Immich unless you enjoy pain — it breaks mobile app login. Immich's own authentication is the gate.

## 6. Verify

1. `systemctl list-timers` shows the next sync.
2. Trigger a scan and tail the application log (see "Where the logs live" below) — healthy output:
   ```
   Starting disk crawl of 2 import path(s)...
   Crawled 10000 file(s) so far...
   Finished disk crawl, 19351 file(s) found on disk and queued 19351 file(s) for import
   Imported 10000 (10000 done so far) file(s)...
   ```
3. Immich Jobs page: metadata extraction → thumbnails → ML queues filling and draining. **Photos only appear in the timeline after metadata extraction**, so a freshly imported library shows nothing for a while — that's the queue, not a fault. On CPU-only hardware the ML pass (CLIP, faces, OCR) over a large library takes **days**.
4. Public URL loads over mobile data.
5. End-to-end: photo on phone → Nextcloud auto-upload → next sync → visible in Immich.

## Where the logs live (community-scripts install)

The services are `immich-web` and `immich-ml` — there is **no** `immich-server` unit, and application output does **not** go to the journal:

```bash
tail -f /var/log/immich/web.log    # API, microservices, library scan output
tail -f /var/log/immich/ml.log     # machine learning
```

`journalctl -u immich-web` only shows systemd start/stop lines. Your timer's rsync output, by contrast, *does* land in the journal: `journalctl -u nextcloud-photo-sync.service`.

## Updating Immich

Immich ships breaking changes often — read the [release notes](https://github.com/immich-app/immich/releases) before every update.

1. Check release notes for anything flagged breaking.
2. Take a snapshot/backup of the container first (`vzdump <ctid> --mode snapshot` on the Proxmox host). Immich's own nightly DB dump is a second layer, not a substitute.
3. Inside the LXC, run the community-scripts updater:
   ```bash
   update
   ```
4. Verify the new version in the admin panel, check `/var/log/immich/web.log` for a clean start, and run a library scan.

Major versions that change ML models will re-queue smart search / facial recognition over the whole library — budget days of CPU churn.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Invalid import path (ENOENT)` when adding the library | Folder doesn't exist yet (or isn't volume-mapped, on Docker). `mkdir -p` first. |
| Scan completes but **0 assets**; log shows `Unable to run job handler (LibrarySyncFilesQueueAll): EACCES: permission denied, scandir '...'` | One or more directories aren't readable by the `immich` user — and the crawl aborts on the *first* one it hits. `chmod -R a+rX /mnt/storage/photos`, re-scan, and make sure `--chmod=D755,F644` is in your rsync flags. |
| `chmod` during a live sync: `cannot access '....XXXXXX': No such file or directory` | Race with rsync's hidden temp files (it writes `.name.RANDOM` then renames). Harmless — re-run after the sync finishes. |
| Storage usage climbing but no photos in timeline | Storage reflects disk (rsync progress), not imports. And imported assets only appear after metadata extraction — check the Jobs page for the queue draining. |
| `rsync: command not found` | Install on both ends — minimal images ship without it. |
| Sync runs but Immich shows nothing | Trigger a manual library Scan; tail `web.log`; confirm files landed (`ls`) and are readable (`sudo -u immich ls ...`). |
| Watch not picking up changes | Large trees can exhaust inotify watches — check the log, fall back to cron-only. |
| Garbage like `1;2c0;276;0c` at the LXC console prompt | Cosmetic terminal escape noise from the Proxmox web console. Press Enter, or just SSH in. |

## References

- [Immich documentation](https://immich.app/docs)
- [Immich External Libraries](https://immich.app/docs/features/libraries)
- [Immich releases](https://github.com/immich-app/immich/releases)
- [community-scripts Immich LXC](https://community-scripts.github.io/ProxmoxVE/scripts?id=immich)
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- [Nextcloud SMB External Storage](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/external_storage/smb.html)

---

*Built on a two-node Proxmox cluster with a 45Drives/Houston NAS, but nothing here is specific to either — any Debian-ish NAS reachable over SSH works. The 19,351-file initial import and the EACCES war story are real.*
