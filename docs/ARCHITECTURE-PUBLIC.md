# Core Homelab Architecture Reference — Proxmox cluster

> **🌐 PUBLIC / SANITISED VERSION.** Compact reference companion to `proxmox-infrastructure-PUBLIC.md`. Addresses are placeholders (`10.0.0.0/24` management, `10.0.1.0/24` storage fabric, `example.com` domains); MACs, tailnet IPs, internal domains and account names removed. Guest IDs and hostnames are real. Reflects the estate as of **12 October 2026**.
## Network Topology
| Network | Subnet | Medium | Purpose |
|---|---|---|---|
| Management / services | 10.0.0.0/24 | 1 GbE, `vmbr0` per node (single bridge port each) | Guest traffic, web UIs, SSH, corosync **link0** |
| Storage fabric | 10.0.1.0/24 | 10 GbE, MTU 9000 (verified all nodes), unmanaged switch, **non-routed** | NFS (media, Paperless, Gitea, pve3 backups), migration/replication, corosync **link1** |
| Tailscale overlay | 100.64.0.0/10 | WireGuard mesh | Admin (pve host node (tailnet IP)) + family access via `slievemore` (exit node + subnet router) |

Gateway 10.0.0.1 (consumer mesh router). **No inbound port-forwards** (WAN 80/443 forward to the old Nextcloud VM confirmed removed, Oct 2026). Flat /24s; VLAN segmentation remains a future item.

DNS: Pi-hole pair (CT106 primary, CT104 secondary). Custom dnsmasq lines on both: two `local=/<internal-domain>/` entries. **No split-DNS overrides for the public hostnames** — LAN clients reach public services via Cloudflare like external clients. Node FQDN domain: `<internal>.local`.

## Node Inventory
| Node | Hardware | RAM / ARC max | Mgmt IP | Storage IP | Pool `tank` |
|---|---|---|---|---|---|
| `pve` | HPE MicroServer Gen10, Opteron X3421 (4c) | 30 GiB / 10 GiB | 10.0.0.30 (`enp3s0f1`) | 10.0.1.30 (`ens2`) | 4×16 TB RAIDZ1, 58.2 T raw, ~25 T free. NFS exporter on fabric. Boot/`local-lvm`: Samsung 870 QVO 1 TB |
| `pve2` | HPE MicroServer Gen10, Opteron X3421 (4c) | 30 GiB / **6 GiB** (cut 11 Oct 2026 — host was overcommitted and swapping guests) | 10.0.0.20 (`enp3s0f1`) | 10.0.1.20 (`ens2`) | 4×4 TB RAIDZ1, 14.5 T raw, ~4.9 T free (5.44 T before the 11 Oct pve3 dump mirror took ~0.5 T) |
| `pve3` | Minisforum MS-02 Ultra, Core Ultra 9 285HX (24c), Arc A310 | 62 GiB / **8 GiB** (cut 11 Oct 2026 for Wazuh headroom) | 10.0.0.25 (`nic0`) | 10.0.1.40 (`nic1`) | **Single** Sabrent Rocket 4.0 1 TB NVMe (no redundancy; 34,344 power-on hours, 108 unsafe shutdowns at 11 Oct 2026), ~784 G free; second NVMe to be added as a mirror once NVMe prices stabilise. Boot: Micron 512 GB |
| `eagle` | Raspberry Pi 4 (non-cluster), Raspberry Pi OS 13 (trixie), kernel `rpi-v8` | — | 10.0.0.50 | — | Uptime Kuma (Docker, network `uptime-kuma_default`, gw 172.18.0.1) + Pimoroni Clipper HAT Mini PIM717 (A7683E) LTE SMS; SIM on auto top-up. **No sshd** (disabled 12 Oct 2026); admin via Raspberry Pi Connect. `unattended-upgrades` on; journal persistent |

Host memory policy: `vm.swappiness = 10` via `/etc/sysctl.d/90-swappiness.conf` (Proxmox Admin Guide, *SWAP on ZFS*) — applied pve2 11 Oct 2026; confirm pve and pve3. ARC caps in `/etc/modprobe.d/zfs.conf` + `update-initramfs -u -k all`.

All nodes: **PVE 9.2.21**, kernel 7.0.14-17-pve, ZFS 2.4.4, pve-ha-manager 5.2.5. Cluster: 3 nodes / 3 votes / quorum 2; corosync knet, `link0` mgmt + `link1` fabric, passive link mode.

`datacenter.cfg`: `migration: type=insecure,network=10.0.1.0/24` (migration and replication on the fabric). Cleaned 8 Oct 2026 — previously held a duplicate `migration:` line and a pasted copy of the INTERNAL doc.

### Storage fabric baseline (11 Oct 2026)
iperf3 pve2 → pve, `-P 4 -t 10`: **6.2 Gbit/s**, flat across the run, 47 retransmits in 7.2 GB — CPU-bound on the Opteron X3421, not link-limited. 8972-byte DF ping pve2 → pve succeeds (0.24 ms RTT). Disks, not the fabric, bound replication/NFS throughput. pve3 fabric not yet tested.

## Storage Model
- Pool on every node is `tank`. `vmstorage` = cluster-spanning storage ID (`nodes pve,pve3,pve2`, `sparse 1`) — use it for anything that replicates or fails over. Legacy IDs: `tank`/`vmstoragelimited` (pve only), `tank2`/`vmstorage2` (pve2 only).
- `local-lvm` still holds CT101 rootfs and the retired VM111 disks on pve — **not replicable**.
- Backup storages: `node1_backup` (pve, `/tank/backups`), `node2_backup` (pve2, `/tank/backups`), `node3_backup` (pve3, NFS from pve over the fabric).
- pve3 NFS client mounts from pve (fabric): `/mnt/media`, `/mnt/paperless`, `/mnt/gitea-repos`, `node3_backup`.

### Data protection
| Layer | Scope |
|---|---|
| vzdump (snapshot, zstd, keep-last 4 / monthly 6 / yearly 1) | pve **Sat 01:00** → `node1_backup`; pve2 **Sun 01:00** → `node2_backup`; pve3 **Mon 01:00** → `node3_backup`. Jobs are node-scoped: a guest running on another node is skipped (CT121/CT141 are listed in both pve and pve2 jobs for this reason). CT134 deliberately excluded (stateless) |
| pvesr replication | CT121, VM113: pve2 → pve (*/5). CT131, CT132: pve2 → pve3 (*/5). CT133: pve2 → pve3 (*/15) |
| sanoid | pve: `tank/storage`, `tank/archive`, `tank/Podcasts`, `tank/subvol-121-disk-1`; pve2: `tank/subvol-112-disk-2` (Immich). Replica datasets prune-only |
| syncoid (nightly ~02:00) | pve2 pulls `tank/storage` → `tank/replica/storage`; pve pulls Immich → `tank/replica/immich-library` |
| dump-mirror (rsync over the fabric, Kuma push per run) | pve2 **Sat + Mon 06:00**: pve `/tank/backups/dump/` → `/tank/replica/node1-dumps` (plain dir), then pve3 `/tank/backups/pve3/dump/` → `tank/replica/node3-dumps` (dataset, 1M recordsize, compression off; added 11 Oct 2026, ~501 G). pve **Sun 06:00**: pve2 dumps → `/tank/replica/node2-dumps`. Every node's vzdumps now exist on two pools |
| Nextcloud DB dump | pve2 02:45 → `tank/nextcloud-dbdump` (zstd, 7 days local) |
| restic → EU storage box (~03:00, per-node repos) | pve: `tank/storage`. pve2: Immich (`subvol-112-disk-2`, thumbs/encoded-video excluded) + Nextcloud data (`subvol-131-disk-1`, previews excluded) + `tank/nextcloud-dbdump` (separate snapshot group, tag `nextcloud`). Retention 7d/4w/6m/1y, 2 % read-data check per run |

### SMART / drive health (added 11 Oct 2026)
| Node | Drives | smartd `-s` | `-W` (diff,info,crit °C) | Kuma push monitor |
|---|---|---|---|---|
| pve | 5 SATA (870 QVO + 4× Exos) | S daily 02:00, L Wed 03:00 | 4,50,58 | `PVE1 - smart-pve1` |
| pve2 | 5 SATA (SK hynix + 4× HPE) | S daily 02:00, L Thu 03:00 | 4,50,58 | `PVE2 - smart-pve2` |
| pve3 | 2 NVMe (Micron + Sabrent) | S daily 02:00, L Sat 03:00 | 4,60,70 | `PVE3 - smart-pve3` |

- Directive: `DEVICESCAN -d removable -a -n standby,q -s (...) -W (...) -m root -M exec /usr/share/smartmontools/smartd-runner`. Backup: `/etc/smartd.conf.bak-20261011`.
- Event path: smartd → `smartd-runner` → `/etc/smartmontools/run.d/20kuma` (push `status=down` with device + message); stock `10mail` retained.
- Heartbeat: `smart-kuma-heartbeat.timer` 06:15 daily (+5 min jitter, `Persistent=true`) → `/usr/local/sbin/smart-kuma-heartbeat` (`smartctl -H` every drive + smartd liveness → up/down). Kuma heartbeat interval 90000 s; Upside Down Mode off.
- Token: `/etc/smartmontools/kuma.env` (600 root:root), separate token per node. Never in scripts or docs.
- HDD thresholds 50/58 rather than 45/55: Exos idle at 40–42 °C (rated 60 °C); lower values would alert on every scrub.
- pve3 Sabrent NVMe error-log count (~8,225) is benign — admin-queue `0x2002` Invalid Field entries from host probes; `media_errors` 0. smartd classes them as ignored.
- `nvme-cli` + `lm-sensors` installed on pve3 only.

### HA
| Resource | Rule | Nodes |
|---|---|---|
| CT121 `bencrom` | `aff-vault` (node-affinity) | pve:2, pve2:1 · `failback 0` |
| VM113 `breifne` | `aff-hass` (node-affinity) | pve:1, pve2:2 · `failback 0` |
| CT131, CT132, CT133 | `nextcloud-nodes` (node-affinity, equal weight) + `nextcloud-together` (positive resource-affinity, CT131+CT132) | pve2, pve3 · `max_restart 2`, `max_relocate 1` |

PVE 9 constraint: positive resource-affinity cannot combine with a node-affinity rule that uses differing priorities.

## Guest Inventory
### pve
| ID | Name | IP | Service |
|---|---|---|---|
| CT101 | `oriel` | .32 | NAS / Samba (rootfs on `local-lvm`) |
| CT103 | `iveagh` | .34 | Transmission — **retirement candidate** (superseded by SABnzbd + Sonarr/Radarr/Seerr); unprivileged (audit 11 Oct 2026 — every LXC on the cluster is unprivileged); `unused0` disk |
| CT106 | `commedagh` | .10 | Pi-hole primary |
| CT114 | `shanlieve` | .35 | SABnzbd |
| CT115 | `doan` | .36 | Sonarr |
| CT116 | `errigal` | .37 | Radarr |
| CT119 | `meelmore` | .31 | Audiobookshelf + podcasts (Tailscale in CT) |
| CT120 | `meelbeg` | .29 | Shelfmark (books) |
| VM111 | `mweelrea` | (.40) | **Retired 9 Oct 2026** — stopped, `onboot 0`, snapshot `pre-cutover-20261009`; destroy after 23 Oct 2026 |

### pve2
| ID | Name | IP | Service |
|---|---|---|---|
| CT104 | `donard` | .11 | Pi-hole secondary |
| CT105 | `slievemore` | .22 | Tailscale exit node + subnet router |
| CT109 | `knockbrinnea` | .23 | Mealie (Ubuntu 24.04) on its own port only — legacy nginx/certbot and rsyslog disabled Oct 2026 |
| CT112 | `immich` | .21 (static) | Immich + tunnel connector (route to `localhost`); hourly photo pull from the NAS with a read-only, source-pinned rsync key |
| CT117 | `conavalla` | .38 | Prowlarr |
| CT118 | `beara` | .39 | Seerr (Tailscale in CT) |
| CT121 | `bencrom` | .42 | Vaultwarden + `ulster-vault` tunnel connector (HA) |
| CT131 | `nephin` | .48 | Nextcloud 35 app — nginx + PHP-FPM 8.4 + Redis (socket) (HA) |
| CT132 | `knockmore` | .49 | MariaDB 11.8 for Nextcloud (HA); **swap 0** (DB must not swap) |
| CT133 | `croaghaun` | .12 | `ulster-cloud` tunnel connector #1 (HA) |
| CT141 | `eurooffice` | .41 | Euro-Office DocumentServer (Debian 12) |
| VM113 | `breifne` | .207 | Home Assistant (HA); 4 GiB, balloon 2 GiB (was 8 GiB; ~1.5 GB used) |

### pve3
| ID | Name | IP | Service |
|---|---|---|---|
| CT122 | `binnian` | .33 | Plex (Arc A310 `renderD129`; media via NFS) |
| CT123 | `bearnagh` | .13 | Hybrid LLM chat server |
| CT124 | `lamagan` | .14 | AnythingLLM (Docker-in-LXC; bge-m3) |
| CT125 | `chimneyrock` | .15 | Immich ML — OpenVINO on Arc A310 |
| CT126 | `carn` | .16 | Paperless-ngx (NFS `/mnt/paperless`; Tailscale in CT) |
| CT127 | `spelga` | .17 | Gitea (NFS `/mnt/gitea-repos`) |
| CT129 | `pigeonrock` | .19 | ACME certificate issuer (resolver 1.1.1.1, not Pi-hole); consumers pull over SSH with forced-command, source-pinned keys |
| VM130 | `gullion` | .44 | **Wazuh SIEM 4.14.8** all-in-one (manager + indexer + dashboard), Ubuntu 24.04; 4c / 8 GiB (balloon 0) / 120 G; built 11 Oct 2026 (replaced test CT130 fitness app). Dashboard https://10.0.0.44 |
| CT134 | `beenkeragh` | .18 | `ulster-cloud` tunnel connector #2 (pinned, not HA) |
| VM150 | `winmigrate` | — | Windows 11 (iGPU passthrough `0000:00:02`); 250 G `scsi0`; 8 GiB, balloon 4 GiB (was 16 GiB, cut 11 Oct 2026); old clone removed; Windows 11 Enterprise, **activated** (Oct 2026) |

Fedora workstation `fedhome` (RX 7900 XTX): **Ollama only**. Interfaces: `enp5s0` 10.0.0.45 (wired, primary), `wlp4s0` 10.0.0.46 (Wi-Fi, also up), `enp6s0` unused, Tailscale node. Previously documented as .150 — references to .150 (e.g. chat-server Ollama endpoint) are stale. No SSH server — pull files from it, never push.

## Operations
| Item | Detail |
|---|---|
| Patching | The Ultimate Updater (formerly BassT23) from pve root crontab: `update` daily 10:00 and `update -s` nightly 03:00, cluster-wide; takes `Update_<timestamp>` guest snapshots. `eagle` (outside the updater): `unattended-upgrades` — Debian, Debian-Security, Raspberry Pi Foundation origins; `wazuh-agent` blacklisted; Docker excluded; no auto-reboot (`/etc/apt/apt.conf.d/52unattended-upgrades-local`) |
| Monitoring | Uptime Kuma + LTE SMS on `eagle` (SMS via `kuma-sms-bridge`); `ha-health-push.timer` (pve, pve2); `arr-monitor.timer` (pve, 15 min); `smart-kuma-heartbeat.timer` (all nodes, 06:15); `dump-mirror.service` Kuma push (pve, pve2); **Wazuh** — see *Wazuh SIEM* below |
| Test tooling | `iperf3` installed on pve and pve2, **daemon disabled** 11 Oct 2026 (was listening on `*:5201`); not installed on pve3. Ad-hoc only: `iperf3 -s -1 -B <fabric IP>` on the target plus a temporary `/etc/pve/nodes/<node>/host.fw` rule (`IN ACCEPT -i ens2 -source <peer fabric IP> -p tcp -dport 5201`), deleted after the test |

### Wazuh SIEM (VM130 `gullion`, 4.14.8)
| Item | Detail |
|---|---|
| Agents | **29** (+ manager = 30 Active, 12 Oct 2026): nodes 001–003, `eagle`, `fedhome`, every CT except **CT132** (2 GiB, swap 0), **CT122** (memory cap; re-assess ~19 Oct) and **CT103** (retiring). All pinned `4.14.8-1` + `apt-mark hold` (packages held on the server too — agent must never be newer than the manager); enrolment password required |
| Groups | `default` (all); `proxmox-nodes` (001–003) — FIM + auditd shipping, below |
| FIM (`proxmox-nodes`) | realtime + diffs: `/etc/network`, `/etc/kernel`, `/etc/default`, `/etc/modprobe.d`, `/etc/sysctl.d`, `/etc/cron.d`; **whodata** + diffs: `/etc/ssh` (host-key `nodiff`); realtime no-diff: `/var/spool/cron/crontabs`; scheduled no-diff: `/etc/pve` (ignores dotfiles, `lrm_status`, `manager_status`, `priv/lock`) |
| auditd (nodes) | `/etc/audit/rules.d/50-pve.rules`, 13 rules: identity, time + modules (`auid!=unset`), network, sshd config, cron. No `execve`, not immutable. `audit.log` collected via group `localfile` |
| sshd (nodes) | `/etc/ssh/sshd_config.d/50-hardening.conf`: `PermitRootLogin prohibit-password`, MaxAuthTries 4, LoginGraceTime 60, ClientAlive 300×3, MaxStartups 10:30:60, ETM MACs (forwarding kept for clustering) |
| Email alerts | Level ≥12 → `alerts@example.com` via Postfix null-client on VM130 (loopback-only, `[smtp.example.net]:465`, SASL `/etc/postfix/sasl_passwd` 600 root, dedicated app password); from `wazuh@example.com`; cap 12/h |
| Retention / capacity | ISM `wazuh-alerts-90d`. With balloon 0, PVE shows ~100 % memory (page cache) — judge by in-guest `free -m` *available* (5 GiB on 12 Oct) |
| Local tuning (18 Oct 2026) | `default` group ignores: rootcheck `^/dev/\.lxc`; syscheck `^/etc/pihole/gravity`, `^/etc/lvm/archive/`, `^/etc/ultimate-updater/`. `local_rules.xml`: 100110 (promiscuous mode on `tap`/`veth`/`fw*` → 0), 100120–100123 (root SSH from node/fabric IPs → 0; PAM 5501/5502 kept). Pre-tuning baseline ~180k alerts/week, ~75 % cluster-internal SSH |
| Monitored by | Kuma `SIEM (gullion)` — HTTPS keyword, TLS ignored |

### `eagle` alerting path (11 Oct 2026; modem power fixed 12 Oct 2026)
| Component | Detail |
|---|---|
| `clipper-power.service` | Oneshot, `After=/Wants=dev-serial0.device` (not `multi-user.target` — that formed an ordering cycle). `/opt/clipper-watchdog/clipper-power.py`: drives **RESET (BCM 24) high**, probes AT with the port held open (~37 s), pulses **PWRKEY (BCM 6)** low 1 s at most once, exits 1 if silent. Pins per PIM717 schematic; GPIO 8 is not connected to the modem. Survives soft reboot (verified) |
| `wan-watchdog.service` | Pings 1.1.1.1/9.9.9.9 every 60 s (threshold 3), SMS on WAN loss; `Restart=always`. `--test` sends a test SMS |
| SMS test | `sudo systemd-run --pty --wait -p EnvironmentFile=/etc/clipper-watchdog/watchdog.conf /usr/bin/python3 /opt/clipper-watchdog/wan-watchdog.py --test` (the bare script lacks the environment) |
| `kuma-sms-bridge.service` | Kuma Webhook → SMS. `/opt/clipper-watchdog/kuma-sms-bridge.py`, binds `172.18.0.1:8099` only; `X-Bridge-Token` header (`/etc/clipper-watchdog/bridge.env`, 600 root); 10 SMS/h cap, 5-min dedup per monitor+state; ASCII, 155 chars |
| Modem lock | `send_sms()` takes `flock` on `/run/lock/clipper-sms.lock`; daemon, `--test` and bridge serialised. Backup `wan-watchdog.py.bak-20261011` |
| Kuma notification | `eagle SMS bridge` (Webhook) — enabled on essential monitors only: PVE hosts, NAS, Cloudflare, Euro-Office, gateway, `smart-pve`/`smart-pve2`/`smart-pve3`. Not default-enabled (overnight updater noise) |

## Ingress Paths
- **Public blog:** https://richhacks.blog → Cloudflare Pages (Astro v7 + MDX, pnpm).
- **Cloudflare tunnels** (all routes point at LAN IPs, never `localhost`):

| Tunnel | Connectors | Routes |
|---|---|---|
| `ulster-cloud` | CT133 `croaghaun` (.12, pve2/HA) + CT134 `beenkeragh` (.18, pve3) | `cloud.example.com` → `http://10.0.0.48:80`; `office.example.com` → `http://10.0.0.41:80` |
| `ulster-immich` | inside CT112 | `photos.example.com` |
| `ulster-vault` | inside CT121 | `vault.example.com` (YubiKey-gated Access) |

- **Nextcloud edge controls:** no Access application; Cloudflare WAF custom rules (`.php` allow-list, scanner paths, non-GB/IE login challenge) + login POST rate limit; **Bot Fight Mode must stay off** (breaks sync clients). Web updater deliberately blocked — upgrade with `occ` only.
- **Admin:** Tailscale → node web UI/SSH. Datacenter firewall: LAN + tailnet to 8006/22 on all three nodes, fabric allowed, default DROP. No node-level `host.fw` files in steady state. Tailnet ACL policy still not pushed. **Exception:** `eagle` is administered via Raspberry Pi Connect (third-party relay; out-of-band by design so it works when LAN/Tailscale is down); sshd disabled; Raspberry Pi ID protected with MFA.

## Guest Firewalls (per-guest rules in force)
| Guest | Inbound allowed (default DROP) |
|---|---|
| CT131 `nephin` | 80/tcp from .12, .18 (connectors), .41 (Euro-Office), .50 (`eagle` Uptime Kuma); ICMP from LAN |
| CT132 `knockmore` | 3306/tcp from .48 only; ICMP from LAN |
| CT141 `eurooffice` | 80/tcp from .12, .18 (connectors), .48 (Nextcloud internal URL), .50 (`eagle` Uptime Kuma); ICMP from LAN — added 11 Oct 2026 |
| VM130 `gullion` | 1514–1515/tcp (agents), 443/tcp (dashboard), 22/tcp, ICMP — all from 10.0.0.0/24 (covers tailnet via CT105 SNAT and `eagle`). API 55000 and indexer 9200/9300 not exposed — added 11 Oct 2026 |

Other guests with `firewall=1` on the NIC have no rule file, so no guest firewall is enforced. **Any new guest firewall must include `10.0.0.50` if Uptime Kuma monitors that guest** — dropped probes stall Kuma's backend until they time out.

## Naming & Conventions
- Guests named after **Irish mountains** (Mourne, Mayo, Donegal and others) — hostname decoupled from function.
- One service per container; unprivileged LXCs (UID offset 100000 — container `www-data` 33 = host 100033).
- Usenet completes land inside `tank/Media` for atomic imports; churn in `tank/scratch`, no snapshots.
- Every tunnel connector IP must be added to the origin's firewall, nginx `set_real_ip_from`, and Nextcloud `trusted_proxies`.
