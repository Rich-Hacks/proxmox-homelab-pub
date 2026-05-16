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
