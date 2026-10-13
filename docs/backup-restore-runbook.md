# Backup & restore runbook — Proxmox VE 9 cluster `ulster`

| | |
|---|---|
| **Maintainer** | Richard Carragher (RC COMMS) |
| **Last reviewed** | 12 October 2026 (v2.2 — site-loss stance: no PBS; off-cluster restore drill) |
| **Companion documents** | `proxmox-infrastructure-INTERNAL.md` (§Storage, §Data protection), `nextcloud-INTERNAL.md` (§4, §6), `learnings-and-gotchas.md` |
| **Audience** | The future you, at 02:00, with one node down and coffee not yet brewed |

> **v2.0 changes.** Rewritten for the three-node cluster (pve3 joined July 2026), the pve2 pool rename (`tank2` → `tank`; paths on pve2 are now `/tank/...`), the per-node backup storages (`node1_backup`, `node2_backup`, `node3_backup`), pvesr/HA, the Nextcloud CTs (131–134) with their DB dump and restic split, and the retirement of VM 111. All `zfs_backup` examples are gone — that storage is no longer a backup target.

---

## Table of contents

1. [What is backed up, and where](#1-what-is-backed-up-and-where)
2. [Schedule](#2-schedule)
3. [Take an ad-hoc backup right now](#3-take-an-ad-hoc-backup-right-now)
4. [Restore: full guest from vzdump](#4-restore-full-guest-from-vzdump)
5. [Restore: single file from inside a backup](#5-restore-single-file-from-inside-a-backup)
6. [Restore: roll back to a snapshot](#6-restore-roll-back-to-a-snapshot)
7. [Restore: ZFS dataset from snapshot](#7-restore-zfs-dataset-from-snapshot)
8. [Restore: Nextcloud](#8-restore-nextcloud)
9. [Disaster scenarios](#9-disaster-scenarios)
10. [Test-restore drills](#10-test-restore-drills)
11. [Retention & pruning](#11-retention--pruning)
12. [Replication & off-site stack](#12-replication--off-site-stack)
13. [Monitoring & verification](#13-monitoring--verification)

---

## 1. What is backed up, and where

### Layers of protection

| Layer | What it captures | Frequency | Lives on |
|---|---|---|---|
| **vzdump** | Full guest backup (config + rootfs + mount points flagged `backup=1`), zstd | Weekly, per node | `node1_backup` (pve, `/tank/backups`), `node2_backup` (pve2, `/tank/backups`), `node3_backup` (pve3 → NFS from pve `/tank/backups/pve3` over the fabric) |
| **Proxmox snapshot** | Point-in-time of a single guest | On demand before changes; Ultimate Updater takes `Update_<timestamp>` snapshots | Same storage as the guest |
| **pvesr replication** | ZFS-level copy of a guest's volumes to a second node — feeds HA recovery | Every 5–15 min | CT121, VM113 between pve and pve2; CT131/132/133 pve2 → pve3 |
| **sanoid ZFS snapshots** | Block-level history of critical datasets | Every 15 min timer; 14d/8w/6m/1y (`critical`), 7d/4w/2m (`archive`) | Same pool as the dataset |
| **syncoid cross-node replicas** | Snapshot history of each Gen10 node's critical dataset on the other Gen10 node | Nightly ~02:05 (pull) | pve2 `tank/replica/storage`; pve `tank/replica/immich-library` (607 G, current — verified 11 Oct 2026) |
| **dump mirrors** | rsync copy of each node's vzdump directory to a second pool | Weekly, ~5 h after each job (pve2 Sat + Mon 06:00, pve Sun 06:00) | pve2 `/tank/replica/node1-dumps` (pve's dumps) and `tank/replica/node3-dumps` (pve3's dumps); pve `/tank/replica/node2-dumps` |
| **Nextcloud DB dump** | `mariadb-dump --single-transaction` of CT132, zstd | Nightly 02:45 | pve2 `/tank/nextcloud-dbdump` (7 days local) |
| **restic off-site** | Encrypted, deduplicated backup to Hetzner Storage Box BX11 (Falkenstein) | Nightly ~03:00 | `restic-pve` / `restic-pve2` repos + Hetzner-side daily box snapshots |

> Dump-mirror units verified 11 Oct 2026 (`systemctl cat dump-mirror.service`): both pull over the fabric (`10-fabric.conf`, 10.10.20.20 / 10.10.20.30) and report to Uptime Kuma (`20-kuma.conf`). pve's base unit still names `root@pve2:/tank2/...` but is overridden by the drop-in (cosmetic tidy on the backlog). pve2's `node1-dumps` is a plain directory inside `tank/replica`; `node3-dumps` is a child dataset (`recordsize=1M`, compression off).

### vzdump storage layout

```
/tank/backups/                     ← pve: node1_backup   |  pve2: node2_backup
└── dump/
    ├── vzdump-lxc-101-2026_10_10-01_00_03.tar.zst    ← CT 101 oriel (NAS)
    ├── vzdump-lxc-132-2026_10_11-01_20_11.tar.zst    ← CT 132 knockmore (pve2)
    ├── vzdump-qemu-113-2026_10_11-01_30_45.vma.zst   ← VM 113 breifne (pve2)
    └── ...
/tank/backups/pve3/dump/           ← on pve's pool, NFS-mounted on pve3 as node3_backup
```

- Filenames are `vzdump-<type>-<id>-<YYYY_MM_DD>-<HH_MM_SS>.<ext>`. Containers → `.tar.zst`, VMs → `.vma.zst`; each has a `.log` and `.notes` alongside.
- `/tank/backups` is a plain directory, not a dataset — deliberately outside sanoid and restic scope.

### What is *not* covered (and what to do about it)

| Item | Why it's a gap | Mitigation |
|---|---|---|
| `/etc/pve/*` | pmxcfs replicates it across all three nodes, but total loss takes it too | Weekly config-backup script → `/root/backup_configs/`; `pve-export.sh` inventory exports kept with project tooling |
| CT 101 data mounts, other bind-mount guests (101, 103, 114, 115, 116, 119, 120, 122) | vzdump backs up rootfs only; data lives in host datasets | Covered by the dataset policy (`tank/storage` 3-2-1; `tank/Media` replaceable) |
| Nextcloud data volume (`tank/subvol-131-disk-1`) | `backup=0` on CT131 `mp0` — too large and churny for weekly dumps | pvesr → pve3 every 5 min + restic (tag `nextcloud`) |
| Immich library (`tank/subvol-112-disk-2`, ~607 G) | Too large for weekly dumps | sanoid + syncoid replica on pve (`tank/replica/immich-library`) + restic |
| pve3 vzdumps | Land on pve's pool (`/tank/backups/pve3`); not restic'd | **Closed 11 Oct 2026:** pve2 mirrors them to `tank/replica/node3-dumps` (`30-node3.conf`, Mon 06:00 timer run); first sync 501 G, verified file-for-file |
| `tank/archive` (pve) | Local-only by design | sanoid `archive` template (7d/4w/2m); survives accidental delete, **not** pool loss |
| `tank/Media`, `tank/Downloads` (~15 T) | Replaceable media | ZFS redundancy + quarterly manual offline copy to spare drives (§12). Accepted re-acquisition risk |
| CT 134 `beenkeragh` | Excluded by design — stateless tunnel connector | Rebuild in minutes (`nextcloud-INTERNAL.md` §3.5) |

### Confirming what's covered for a guest

```bash
pct config 131 | grep -E '^(mp|rootfs)'
# rootfs is ALWAYS in vzdump. mpN only with backup=1.
# CT131 mp0 shows backup=0 — correct: the data is covered by pvesr + restic.
```

---

## 2. Schedule

### vzdump jobs (`/etc/pve/jobs.cfg`; snapshot mode, zstd, `repeat-missed 1`, `notification-system`)

| Job | When | Guests | Target |
|---|---|---|---|
| pve weekly | **Sat 01:00** | CT 101, 103, 106, 114, 115, 116, 119, 120, 121, 141 (+ VM 111 until destroyed) | `node1_backup` |
| pve2 weekly | **Sun 01:00** | CT 104, 105, 109, 112, 117, 118, 121, 131, 132, 133, 141, VM 113 | `node2_backup` |
| pve3 weekly | **Mon 01:00** | CT 122, 123, 124, 125, 126, 127, 129, VM 130 (Wazuh), VM 150 | `node3_backup` |

**Jobs are node-scoped: a guest that is not on the job's node at run time is silently skipped.** CT121 and CT141 are listed in both Gen10 jobs for this reason. The Nextcloud CTs (131–133) are only in the pve2 job — while failed over to pve3 they are not vzdumped (pvesr and restic still cover the data). After any failover or manual migration, check the next job's log and take a manual vzdump if a guest was skipped (§3).

### Automated data-protection timers

| Timer / job | Node(s) | When | What |
|---|---|---|---|
| `sanoid.timer` | pve, pve2 | every 15 min | Snapshots/prunes per `/etc/sanoid/sanoid.conf` — pve: `tank/storage`, `tank/subvol-121-disk-1`, `tank/archive`, `tank/Podcasts`; pve2: `tank/subvol-112-disk-2`. Replica datasets prune-only |
| pvesr | cluster | */5 (CT121, VM113, CT131, CT132), */15 (CT133) | Guest volume replication over the fabric (`migration: network=10.10.20.0/24`) |
| `syncoid-pull.timer` | pve, pve2 | ~02:05 | pve2 pulls `tank/storage`; pve pulls the Immich dataset (`--no-sync-snap`) |
| Nextcloud DB dump | pve2 | 02:45 | `mariadb-dump` from CT132 → `/tank/nextcloud-dbdump`; **fails by design if CT132 is not on pve2** |
| `restic-offsite.timer` | pve, pve2 | ~03:00 | pve: `tank/storage`. pve2: Immich + Nextcloud data + DB dump (separate snapshot group, tag `nextcloud`). Then forget/prune + 2 % read-data check |
| `dump-mirror.timer` | pve2 | Sat 06:00 + Mon 06:00 | rsync of pve's `dump/` → `/tank/replica/node1-dumps`, then pve3's `pve3/dump/` → `/tank/replica/node3-dumps` (two `ExecStart`s, sequential; a failure in the first skips the second) |
| `dump-mirror.timer` | pve | Sun 06:00 | rsync of pve2's `dump/` → `/tank/replica/node2-dumps` |
| Ultimate Updater (pve root crontab) | cluster-wide | 03:00 (`update -s`) and 10:00 | ⚠ The 03:00 run overlaps restic and follows the DB dump — reschedule (backlog High) |

Scripts: `/usr/local/sbin/` (`syncoid-pull.sh`, `restic-offsite.sh`). Logs: `/var/log/syncoid-pull.log`, `/var/log/restic-offsite.log`. Restic environment: `/root/.restic-env` (pve, pve2; root-only).

```bash
cat /etc/pve/jobs.cfg                       # vzdump: blocks
pvesh get /cluster/backup --output-format=yaml
pvesr status                                # on each node
ha-manager status
```

---

## 3. Take an ad-hoc backup right now

Run on the node currently hosting the guest, targeting that node's storage:

```bash
# pve
vzdump 101 --storage node1_backup --compress zstd --mode snapshot --notes-template "manual: pre-change"
# pve2
vzdump 132 --storage node2_backup --compress zstd --mode snapshot --notes-template "manual: pre-change"
# pve3 (incl. Nextcloud CTs while failed over)
vzdump 131 132 133 --storage node3_backup --compress zstd --mode snapshot --notes-template "manual: post-failover"
```

**`--mode snapshot`** is right for almost everything: LXC rootfs is ZFS-snapshotted while running; VMs use a QEMU live backup (VM 113 and VM 150 have the guest agent — VM 150 drives Windows VSS). Fallbacks: `--mode suspend`, then `--mode stop`.

Guests with bind mounts cannot be `pct snapshot`-ed; vzdump still works (rootfs only). Watch progress with `tail -f /var/log/vzdump/*.log` or Datacenter → Backup → Job log.

---

## 4. Restore: full guest from vzdump

### Find the backup

```bash
pvesm list node1_backup | sort -k4 -r | head -20         # on pve
pvesm list node2_backup | grep -E 'vzdump-(lxc|qemu)-132-' # on pve2
```

Volume IDs look like `node2_backup:backup/vzdump-lxc-132-2026_10_11-01_20_11.tar.zst`.

**Backup storages are node-scoped** — `node1_backup` is only visible on pve, etc. To restore a pve guest onto pve2, use the dump mirror on pve2 (`/tank/replica/node1-dumps`) by passing the file path directly to `pct restore`/`qmrestore`, or add it temporarily as a `dir` storage limited to pve2.

### Container restore

```bash
# To a new CTID (safest — original stays in place)
pct restore 901 node1_backup:backup/vzdump-lxc-101-2026_10_10-01_00_03.tar.zst \
  --storage vmstorage --hostname oriel-restore --unprivileged 1

# In place (destroys the existing CT — approval required)
pct restore 101 node1_backup:backup/vzdump-lxc-101-2026_10_10-01_00_03.tar.zst \
  --storage local-lvm --force 1
```

Before starting a restored CT:
- `pct config <id> | grep -E '^(mp|rootfs|net)'` — bind mounts to `/tank/...` paths only resolve where those datasets exist (CT 101's data mounts exist only on pve).
- On a test restore, change the IP or disconnect the NIC; restate `hwaddr=` on any `-net0` edit you keep.
- Use `vmstorage` for guests that must replicate or fail over; `local-lvm` exists only on pve and is not replicable.

### VM restore

```bash
# To a new VMID, new MAC/UUID so it can coexist with the original
qmrestore node2_backup:backup/vzdump-qemu-113-2026_10_11-01_30_45.vma.zst 913 \
  --storage vmstorage --unique 1
```

**HA-managed guests (CT121, VM113, CT131–133):** an in-place restore fights the HA manager. Put the resource in `--state disabled` first (`ha-manager set vm:113 --state disabled` — an HA change, so approval), restore with `--force 1`, then re-enable. Restoring to a new ID avoids this.

### GUI path

Datacenter → Storage → `node1_backup`/`node2_backup`/`node3_backup` → **Backups** → Restore. Choose node, storage, ID. Tick *Start after restore* only if mounts and networking are valid on the target node.

---

## 5. Restore: single file from inside a backup

### Container backups (`.tar.zst`)

```bash
cd /tmp
tar --use-compress-program="zstd -d" \
    -tvf /tank/backups/dump/vzdump-lxc-101-2026_10_10-01_00_03.tar.zst | grep smb.conf
tar --use-compress-program="zstd -d" \
    -xvf /tank/backups/dump/vzdump-lxc-101-2026_10_10-01_00_03.tar.zst ./etc/samba/smb.conf
```

### VM backups (`.vma.zst`)

**GUI:** Datacenter → Storage → backup storage → Backups → highlight dump → **File Restore** → browse, tick, Download.

**CLI:**
```bash
cd /tmp
zstd -d /tank/backups/dump/vzdump-qemu-113-2026_10_11-01_30_45.vma.zst -o vm113.vma
vma extract vm113.vma /tmp/vm113-restored/
losetup -P -f --show /tmp/vm113-restored/disk-drive-scsi0.raw   # prints e.g. /dev/loop3
mkdir -p /mnt/restore && mount -o ro /dev/loop3p1 /mnt/restore
# copy out what you need, then:
umount /mnt/restore && losetup -d /dev/loop3
rm -r /tmp/vm113-restored /tmp/vm113.vma
```

Watch free space — a raw extract is the full disk size.

---

## 6. Restore: roll back to a snapshot

Snapshots beat vzdumps for "I broke something in the last five minutes".

```bash
pct listsnapshot 131
qm listsnapshot 113
```

Typical entries: `Update_<timestamp>` from the Ultimate Updater and manual `pre-<change>` snapshots.

```bash
pct stop 131 && pct rollback 131 pre-upgrade-20261011 && pct start 131
qm  stop 113 && qm  rollback 113 Update_20261011_030012 && qm start 113
```

⚠ Rollback destroys all state newer than the snapshot. For HA guests, stop via `ha-manager set ct:131 --state stopped` rather than `pct stop` (HA would restart it). Rolling back a pvesr-replicated guest forces a full re-sync of its replica. For the Nextcloud pair, roll back CT131 and CT132 to snapshots taken together, or restore the DB from the nightly dump (§8).

Delete old snapshots with `pct delsnapshot <id> <name>` / `qm delsnapshot <id> <name>`.

---

## 7. Restore: ZFS dataset from snapshot

For content outside a guest — e.g. files on `tank/storage` deleted via SMB or Nextcloud.

```bash
zfs list -t snapshot -o name,used,creation tank/storage | tail
ls /tank/storage/.zfs/snapshot/                     # hidden dir, always present
cp -a /tank/storage/.zfs/snapshot/<snap>/Clare/path/to/file /tank/storage/Clare/path/to/file
```

Then `occ files:scan --path="/clare/files/Clare/path"` on CT131 so Nextcloud sees it.

Whole-dataset rollback (`zfs rollback tank/storage@<snap>`) destroys later snapshots and diverges the syncoid replica — prefer clone-and-copy:

```bash
zfs clone tank/storage@<snap> tank/restore-tmp
# copy out, then remove the clone (zfs destroy — approval required)
```

---

## 8. Restore: Nextcloud

Full procedures in `nextcloud-INTERNAL.md` §4; summary:

| Need | Source | Where |
|---|---|---|
| Database only (bad upgrade, corrupted table) | `/tank/nextcloud-dbdump/nextcloud-<TS>.sql.zst` (7 days) or restic tag `nextcloud` for older | pve2 |
| Individual user files | `restic restore … --include /tank/subvol-131-disk-1/<user>/files/<path>` | pve2 |
| Family SMB files (`/<Name>/...`) | `tank/storage` snapshots (§7) — not the Nextcloud data volume | pve |
| Whole CTs | pvesr replica on pve3 (HA does this automatically) or vzdump on `node2_backup` | pve3 / pve2 |
| `config.php` secrets | Keeper + CT131 vzdump rootfs | — |

```bash
pct exec 131 -- /usr/local/bin/occ maintenance:mode --on
zstd -dc /tank/nextcloud-dbdump/nextcloud-<TS>.sql.zst | pct exec 132 -- mariadb nextcloud
pct exec 131 -- /usr/local/bin/occ maintenance:mode --off
pct exec 131 -- /usr/local/bin/occ maintenance:data-fingerprint
```

Files restored from restic must be owned `100033:100033` on the host (container `www-data`), followed by `occ files:scan`.

---

## 9. Disaster scenarios

### Scenario A — One guest is corrupted

1. `journalctl -u <service> -b` inside the guest — is it recent?
2. Snapshot rollback if a pre-incident snapshot exists (§6).
3. Otherwise vzdump restore — to a new ID first if in doubt (§4).

**Time to recover:** ~2 min (rollback), 10–30 min (vzdump).

### Scenario B — One node dead, quorum survives (2 of 3 votes)

No `pvecm expected` needed — the two survivors stay quorate.

1. **HA guests recover automatically** after fencing (~2–3 min): CT121 and VM113 between pve and pve2; CT131/132/133 to pve3 from the last pvesr replica (≤5 min data loss for 131/132, ≤15 min for 133). Check `ha-manager status`.
2. **Non-HA guests** on the dead node stay down. Restore the critical ones elsewhere from vzdump:
   - **pve dead** — DNS still answers from CT104 (pve2). `node1_backup` and `node3_backup` are both on pve's pool and unavailable; use the dump mirrors on pve2 (`/tank/replica/node1-dumps` for pve guests, `/tank/replica/node3-dumps` for pve3 guests). CT101's data (`tank/storage`) is available read-only-in-spirit as `tank/replica/storage` on pve2 — don't write to the replica; restore or promote deliberately. **pve3 also loses its NFS mounts** (media, Paperless, Gitea, `node3_backup`) → Plex, Paperless and Gitea degrade although pve3 is up.
   - **pve2 dead** — Pi-hole primary (CT106) still on pve. Nextcloud fails over to pve3 automatically; Immich (CT112) is down — restore its rootfs/config from `/tank/replica/node2-dumps` on pve and the library from the syncoid replica `tank/replica/immich-library` on pve (≤24 h old; restic as fallback). The DB dump timer is on pve2, so no new dumps until pve2 returns.
   - **pve3 dead** — Plex, LLM stack, Paperless, Gitea, ACME, fitness app and VM150 down; dumps are on pve (`/tank/backups/pve3/dump`, mirrored on pve2 `/tank/replica/node3-dumps`), restorable to pve or pve2 (A310-dependent guests lose acceleration). Nextcloud loses its failover target but keeps running on pve2; `ulster-cloud` drops to one connector (CT133).
3. After restoring a guest onto a different node, fix mount points, storage IDs and node-scoped backup job membership.
4. After the outage, apply the Nextcloud post-failover checks (`nextcloud-INTERNAL.md` §6): DB dump fails and the Sunday vzdump skips the CTs while they are on pve3 — run `vzdump 131 132 133 --storage node3_backup` if the outage is long.

**Time to recover:** HA guests minutes; critical non-HA guests 15–45 min.

### Scenario C — Two nodes dead (quorum lost)

1. On the survivor: `pvecm expected 1` — restores quorum so it can act. HA will not recover anything while quorum is lost.
2. Restore guests from whatever dumps are reachable (local pool or dump mirror) as in Scenario B.
3. When a second node returns, quorum restores itself; reset `expected` is not needed after a full cluster recovery.

### Scenario D — Site loss / all nodes destroyed

1. Install PVE 9 on new hardware; reuse a node name so configs match; `pvecm create ulster` later.
2. If any original pool survives: `zpool import tank`, add `/tank/backups` as a `dir` storage, restore in priority order — DNS (CT106/CT104) → NAS (CT101) → Vaultwarden (CT121) → Nextcloud (CT132 then CT131) → everything else.
3. If no pool survives: restic from Hetzner (§12) using the Keeper/paper escrow — `tank/storage`, Immich, Nextcloud data + DB dumps. Guests are rebuilt from runbooks; vzdumps are not off-site.

**Time to recover:** half a day to several days depending on hardware and restic download rate.

**Stance (decided 12 Oct 2026): no Proxmox Backup Server.** Site loss is treated as a rebuild on new hardware from the runbooks, with a redesign where the new hardware or scope warrants it. Guest images are deliberately not kept off-site. Only Tier 1 data goes to Hetzner. Precedent: `fedhome` was rebuilt in a few hours. Revisit only if dedicated hardware outside the cluster becomes available.

This stance holds only while all of the following are true:

1. **Credentials survive the site.** restic repo passphrases (`restic-pve`, `restic-pve2`), the Hetzner sub-account details and the Storage Box console MFA (a second YubiKey or recovery codes) are in Keeper and the paper escrow. A Vaultwarden instance hosted on the cluster does not count.
2. **Documentation survives the site.** The INTERNAL runbooks exist off-cluster, not only in the vault hosted on the cluster. The sanitised copies on GitHub alone are not sufficient.
3. **Off-site restore is proven** from a machine outside the cluster, using only the escrowed credentials (§10).
4. **Tier 2 loss is accepted.** `tank/archive` is local-only and is lost with the site.

### Scenario E — Pool loss on one node

- **pve `tank`** — `tank/storage` from pve2 `tank/replica/storage` (≤24 h) or restic; pve guest dumps from pve2 `/tank/replica/node1-dumps`; pve3 guest dumps from pve2 `/tank/replica/node3-dumps` (≤5 h behind the Monday job); `tank/Media`/`tank/Downloads` back to the last quarterly offline copy.
- **pve2 `tank`** — Nextcloud from pve3 replicas (HA) or restic; Immich library from pve `tank/replica/immich-library` (≤24 h) or restic; CT121/VM113 from their pvesr replicas on pve; pve2 dumps from pve `/tank/replica/node2-dumps`.
- **pve3 `tank`** (single NVMe, no redundancy) — guests from `node3_backup` dumps on pve; Nextcloud replicas regenerate from pve2 automatically once the pool is rebuilt.

---

## 10. Test-restore drills

A backup you haven't restored is a backup that doesn't exist. Quarterly minimum.

### Quarterly drill checklist

- [ ] Restore the latest CT101 vzdump to CTID 901 on pve with `--unprivileged 1`, no mount points, NIC disconnected. Confirm Samba config present. Stop and destroy the test CT (approval).
- [ ] Restore the latest CT132 vzdump to CTID 932 on pve2, NIC disconnected; start MariaDB; import last night's `/tank/nextcloud-dbdump` into it; compare `SELECT COUNT(*) FROM oc_filecache` with live. Destroy.
- [ ] restic restore test on pve and pve2: one directory to `/tmp/restore-test`, `diff -r` against live, delete. Include one file from tag `nextcloud`.
- [ ] Site-loss drill: from `fedhome` (off-cluster), using only Keeper/paper-escrow credentials, `restic snapshots` and restore one file from each of `restic-pve` and `restic-pve2`. Proves §9 Scenario D conditions 1 and 3.
- [ ] File Restore of one file from the latest VM113 dump via the GUI.
- [ ] Replica sizes: `zfs list -o name,used` source vs replica for every syncoid pair (a size mismatch means a broken pull — check on the node itself, not from an export).
- [ ] `zfs list -t snapshot` — sanoid cadence as expected; browse one replica's `.zfs/snapshot`.
- [ ] Planned window only: Nextcloud HA relocate drill (`nextcloud-INTERNAL.md` §6) — pve2 → pve3 and back.
- [ ] Record date and outcome below.

### Drill log

| Date | Drill | Result | Notes |
|---|---|---|---|
| 9 Oct 2026 | Nextcloud DB dump + restore (migration cut-over) | Pass | 172 tables, 411,376 filecache rows verified on CT132 |
| _Due Q4 2026_ | Full quarterly checklist | — | First run under v2.0 |

---

## 11. Retention & pruning

All three jobs use job retention `keep-last=4, keep-monthly=6, keep-yearly=1`. The three backup storages also carry `prune-backups keep-last=4,keep-weekly=4,keep-monthly=6,keep-yearly=1` — storage-level retention also catches **manual** backups, which job retention does not.

```ini
dir: node1_backup
        path /tank/backups
        content backup
        nodes pve
        prune-backups keep-last=4,keep-weekly=4,keep-monthly=6,keep-yearly=1
        shared 1

dir: node2_backup
        path /tank/backups
        content backup
        nodes pve2
        prune-backups keep-last=4,keep-weekly=4,keep-monthly=6,keep-yearly=1
        shared 1

nfs: node3_backup
        export /tank/backups/pve3
        path /mnt/pve/node3_backup
        server 10.10.20.30
        content backup
        nodes pve3
        options vers=4.2
        prune-backups keep-last=4,keep-weekly=4,keep-monthly=6,keep-yearly=1
```

`shared 1` on node1/node2 is inert (single-node scoping) but misleading — tidy at the next storage pass.

Dry-run a prune:

```bash
pvesm prune-backups node2_backup --dry-run --type lxc --vmid 132 \
  --keep-last 4 --keep-weekly 4 --keep-monthly 6 --keep-yearly 1
```

Other retention: sanoid per template (§2); restic `--keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-yearly 1`; Nextcloud DB dump 7 days local; Hetzner box snapshots rotating.

When VM 111 is destroyed (≥23 Oct 2026, approval), remove it from the Saturday job; its remaining dumps on `node1_backup` age out under retention or can be pruned explicitly.

---

## 12. Replication & off-site stack

### Design

Tier 1 (irreplaceable) gets 3-2-1: `tank/storage` on pve (~469 G), the Immich library `tank/subvol-112-disk-2` on pve2 (~505 G), and Nextcloud (`tank/subvol-131-disk-1` + DB dumps) on pve2. Tier 2 (`tank/archive`) is local snapshots only. Tier 3 (`tank/Media`, `tank/Downloads`) is ZFS redundancy plus a quarterly offline copy.

```
pve  ── sanoid ──► tank/storage ─────────── syncoid (pulled by pve2, ~02:05) ──► pve2 tank/replica/storage
pve2 ── sanoid ──► tank/subvol-112-disk-2 ── syncoid (pulled by pve, ~02:05) ──► pve  tank/replica/immich-library
pve2 ── pvesr */5 ──► CT131/132 volumes ─────────────────────────────────────► pve3 (HA failover target)
pve2 ── 02:45 ──► /tank/nextcloud-dbdump
pve  ── restic ~03:00 ──► Hetzner BX11 "restic-pve"   (tank/storage)
pve2 ── restic ~03:00 ──► Hetzner BX11 "restic-pve2"  (Immich; Nextcloud data + dbdump, tag nextcloud)
pve vzdumps  ── rsync Sat+Mon 06:00 ──► pve2 /tank/replica/node1-dumps
pve3 vzdumps ── rsync Sat+Mon 06:00 ──► pve2 /tank/replica/node3-dumps   (stored on pve /tank/backups/pve3)
pve2 vzdumps ── rsync Sun 06:00 ─────► pve  /tank/replica/node2-dumps
```

### Components

- **sanoid** (pve, pve2): `/etc/sanoid/sanoid.conf`. Critical datasets `frequently=0, hourly=0, daily=14, weekly=8, monthly=6, yearly=1`; `tank/archive` on the `archive` template; replica datasets prune-only (`autosnap=no`).
- **syncoid** (pve, pve2): nightly **pull** with `--no-sync-snap`; `/usr/local/sbin/syncoid-pull.sh`. The replica must never be written to.
- **pvesr**: storage `vmstorage` (cluster-spanning) over the fabric; `pvesr status` per node. After an HA failover, replication direction reverses automatically for HA-moved guests.
- **dump mirrors**: `rsync -a --delete` over the fabric; retention follows the vzdump jobs. pve2 holds copies of both pve's and pve3's dumps; pve holds pve2's. Result pushed to Uptime Kuma (`kuma-push %N`) after every run. Drop-ins: `/etc/systemd/system/dump-mirror.service.d/{10-fabric,20-kuma,30-node3}.conf`, `/etc/systemd/system/dump-mirror.timer.d/20-node3.conf` (pve2).
- **restic**: SFTP port 23 to Hetzner BX11 (Falkenstein); one sub-account, repo and passphrase per node. Excludes: Nextcloud previews; Immich `thumbs/` + `encoded-video/`. Each run: `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-yearly 1 --prune` + 2 % `check --read-data-subset`. Nextcloud is a **separate `restic backup` call** tagged `nextcloud`, so its snapshot group is distinct.
- **Hetzner-side controls**: daily automatic box snapshots (unreachable from the LAN credentials), delete/rescale protection, console MFA (TOTP + YubiKey).
- **Quarterly offline media copy**: spare drives (1×4 TB, 3×3 TB) via USB dock, `rsync -a --delete` of `tank/Media` subsets onto ext4.

### Immich replica — verified

The 9 Oct export appeared to show `tank/replica/immich-library` at 1.71 G against a 505 G source. Re-checked on pve on 11 Oct 2026: the replica is **607 G**, matching the source, with the 11 Oct daily snapshot present. The earlier figure was a misattribution in the cross-node export. The replica also receives CT 112's `Update_*` snapshots from the Ultimate Updater; sanoid's prune-only stanza does not remove those, so check their count at audits.

### Key escrow

Restic passphrases, Hetzner console login, MFA recovery codes and the box account password are in **Keeper** (YubiKey) plus an offline paper copy. A restore must never depend on infrastructure that may be down during the disaster.

### Restore from restic (bare-metal case)

```bash
apt install restic
export RESTIC_REPOSITORY="sftp:u<box>-sub1@u<box>.your-storagebox.de:repo"   # sub2 for pve2; port 23 via ~/.ssh/config Host entry
export RESTIC_PASSWORD_FILE=/root/restic-pass      # from Keeper/paper; chmod 600; shred afterwards
restic snapshots                                   # add --tag nextcloud on the pve2 repo for Nextcloud
restic restore latest --target /restore --include "/tank/storage/Clare"
```

If the on-site key is lost, reset the sub-account from the Hetzner console. If ransomware deleted the repo, roll the box back from a Hetzner snapshot first, then restore.

### Restore from the cross-node replica

```bash
# tank/storage lost on pve — recover from pve2
zfs list -t snapshot -r tank/replica/storage | tail -3        # on pve2
zfs send -R tank/replica/storage@<snap> | ssh root@10.10.20.30 zfs receive tank/storage   # fabric, to a rebuilt pool
```

Single files: `cp` from `/tank/replica/storage/...` or its `.zfs/snapshot/<name>/` tree on pve2.

---

## 13. Monitoring & verification

Weekly two-minute check (pve and pve2 unless noted):

```bash
systemctl list-timers sanoid.timer syncoid-pull.timer dump-mirror.timer restic-offsite.timer --no-pager
tail -5 /var/log/syncoid-pull.log
tail -10 /var/log/restic-offsite.log
pvesr status                                   # all nodes: no FAILED, last sync minutes ago
ha-manager status                              # all resources started on the expected nodes
ls -lt /tank/nextcloud-dbdump | head -3        # pve2: last night's dump present
( source /root/.restic-env && restic snapshots --tag nextcloud --compact | tail -3 )   # pve2
```

Replica freshness **and size**:

```bash
zfs list -t snapshot -r tank/replica/storage | tail -2                  # pve2
zfs list -o name,used tank/storage                                      # pve  — compare
zfs list -t snapshot -r tank/replica/immich-library | tail -2           # pve
zfs list -o name,used tank/replica/immich-library                       # pve  — vs tank/subvol-112-disk-2 on pve2
```

PVE notifications (vzdump, pvesr, HA) go to the `Mailbox` SMTP target; weekend "backup OK" mails (Sat, Sun, Mon) are the heartbeat — silence is a signal. Uptime Kuma on `eagle` covers service liveness, not backups.

Dump mirrors also push to Uptime Kuma after every run (`20-kuma.conf`) — a down/missed push is the first sign of a broken mirror. Quick parity check on pve2: `diff <(ssh root@10.10.20.30 'ls /tank/backups/pve3/dump') <(ls /tank/replica/node3-dumps)` → no output.

Failure modes to watch: a timer with no `LAST` run; syncoid "target is newer" (someone wrote to a replica); a replica far smaller than its source; a vzdump log listing a guest as "not on this node"; DB dump unit failed (CT132 not on pve2); Hetzner quota creep toward 1 TB.

---

## Quick reference card

```
# Backup now (on the guest's current node)
vzdump <id> --storage node1_backup|node2_backup|node3_backup --compress zstd --mode snapshot

# List backups for a guest
pvesm list node2_backup | grep -- '-<id>-'

# Restore to new ID / in place
pct restore <new-id> node2_backup:backup/<file> --storage vmstorage --hostname test
qmrestore   node2_backup:backup/<file> <new-id> --storage vmstorage --unique 1
pct restore <ctid> node2_backup:backup/<file> --storage vmstorage --force 1     # approval; disable HA first

# Snapshot rollback (non-HA)
pct stop <id> && pct rollback <id> <snap> && pct start <id>

# ZFS file restore
cp -a /tank/storage/.zfs/snapshot/<snap>/path/to/file /tank/storage/path/to/file

# Nextcloud DB restore (pve2)
zstd -dc /tank/nextcloud-dbdump/nextcloud-<TS>.sql.zst | pct exec 132 -- mariadb nextcloud

# Two nodes down
pvecm expected 1

# Replication / HA
pvesr status ; ha-manager status

# Off-site health
( source /root/.restic-env && restic snapshots --compact | tail -3 )

# All protection timers at a glance
systemctl list-timers sanoid.timer syncoid-pull.timer dump-mirror.timer restic-offsite.timer --no-pager
```

(Substitute every `<…>` before running — bash reads `<` as redirection.)

---

## References

- [Proxmox VE Admin Guide — Backup and Restore](https://pve.proxmox.com/pve-docs/chapter-vzdump.html)
- [Proxmox VE Admin Guide — Storage Replication (pvesr)](https://pve.proxmox.com/pve-docs/chapter-pvesr.html)
- [Proxmox VE Admin Guide — High Availability](https://pve.proxmox.com/pve-docs/chapter-ha-manager.html)
- [`vzdump(1)`](https://pve.proxmox.com/pve-docs/vzdump.1.html) · [`pct(1)`](https://pve.proxmox.com/pve-docs/pct.1.html) · [`qm(1)`](https://pve.proxmox.com/pve-docs/qm.1.html)
- [OpenZFS — snapshots & rollback](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-rollback.8.html)
- [restic documentation](https://restic.readthedocs.io/)
- [Sanoid / Syncoid](https://github.com/jimsalterjrs/sanoid)

## Change history

| Date | Version | Change |
|---|---|---|
| 11 Jun 2026 | 1.x | Two-node runbook; Phase 1 sanoid/syncoid/restic/dump-mirror stack |
| 11 Oct 2026 | 2.0 | Three-node re-baseline: pve3 + `node3_backup`, current job guest lists, `tank2` → `tank` paths, pvesr/HA, Nextcloud DB dump + restic split, failover caveats, node-loss scenarios per node, Immich replica defect, `tank/archive` tier, VM 111 retirement, drills on current guests |
| 11 Oct 2026 | 2.1 | pve3 dumps now mirrored to pve2 `tank/replica/node3-dumps` (drop-in + Mon 06:00 timer); dump-mirror units verified (fabric, Kuma push); §1 gap closed; Immich replica verified complete (607 G) — "partial" warnings removed |
| 12 Oct 2026 | 2.2 | §9 Scenario D: site-loss stance recorded — no PBS, rebuild from runbooks, four holding conditions; §10: off-cluster site-loss restic drill added |

*Update this runbook every time a procedure here diverges from reality. Out-of-date runbooks are worse than no runbook.*
