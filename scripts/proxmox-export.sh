#!/bin/bash
# /root/proxmox-export.sh
HOSTNAME=$(hostname)
DATE=$(date '+%Y-%m-%d %H:%M')

echo "==============================================
 Proxmox inventory export
 Host: $HOSTNAME
 Date: $DATE
=============================================="
echo
echo "=== NODE SUMMARY ==="
pveversion
echo
echo "=== STORAGE POOLS ==="
pvesm status
echo
echo "=== VIRTUAL MACHINES ==="
qm list
for VMID in $(qm list | awk 'NR>1 {print $1}'); do
    echo; echo "--- VM $VMID ---"
    qm config "$VMID"
done
echo
echo "=== LXC CONTAINERS ==="
pct list
for CTID in $(pct list | awk 'NR>1 {print $1}'); do
    echo; echo "--- CT $CTID ---"
    pct config "$CTID"
done
echo
echo "=== NETWORK ==="
cat /etc/network/interfaces
echo
echo "=== CLUSTER ==="
pvecm status 2>/dev/null || echo "(not clustered)"
Appendix B — Configuration backup script
#!/bin/bash
# /root/backup_configs.sh — weekly cron
BACKUP_DIR="/root/backup_configs/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

cp /etc/pve/storage.cfg                "$BACKUP_DIR/"
cp -r /etc/pve/lxc                     "$BACKUP_DIR/"
cp -r /etc/pve/qemu-server             "$BACKUP_DIR/"
cp /etc/network/interfaces             "$BACKUP_DIR/"
cp /etc/hosts                          "$BACKUP_DIR/"

zpool status > "$BACKUP_DIR/zpool_status.txt"
zfs list -t all > "$BACKUP_DIR/zfs_list.txt"
pvesm status > "$BACKUP_DIR/pvesm_status.txt"
pvecm status > "$BACKUP_DIR/pvecm_status.txt"

# On CT 101 (NAS) — also capture Samba state
ssh root@10.10.10.32 'cat /etc/samba/smb.conf' > "$BACKUP_DIR/samba_smb.conf"
ssh root@10.10.10.32 'testparm -s 2>/dev/null' > "$BACKUP_DIR/samba_testparm.txt"

echo "Backup written to $BACKUP_DIR"

