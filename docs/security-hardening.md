# Security Hardening — Mini SCADA on RHEL 10

## firewalld Rules

Ports 5020 (Modbus TCP) and 8080 (Dashboard) are restricted
to localhost only using firewalld rich rules.

### Rules applied:

rule family="ipv4" source address="127.0.0.1" port port="5020" protocol="tcp" accept
rule family="ipv4" source address="127.0.0.1" port port="8080" protocol="tcp" accept
### Commands used:
```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="127.0.0.1" port port="5020" protocol="tcp" accept'
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="127.0.0.1" port port="8080" protocol="tcp" accept'
sudo firewall-cmd --reload
```

### Why this matters in SCADA:
- Modbus TCP (port 5020) must never be exposed to untrusted networks
- NERC CIP and IEC 62443 both mandate network segmentation for OT systems
- In production, source address would be the SCADA master IP, not localhost

## Verification
```bash
sudo firewall-cmd --list-all   # confirm rich rules
ss -tlnp | grep -E '5020|8080' # confirm ports listening
curl http://localhost:8080      # confirm dashboard reachable
```
EOF
