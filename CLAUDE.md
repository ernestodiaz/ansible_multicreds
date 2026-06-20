# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

This is a **Multi-Credential / Multi-Protocol Cisco Login Auditor** built with Ansible. It tests login connectivity across a list of network devices when there is no single shared credential, cycling through an ordered list of credentials and protocols (SSH then Telnet by default) and stopping at the first working combination per device. Results are written to `output/login_results.csv`.

## Dependencies

Install Python dependencies on the Ansible controller (using uv or pip):

```bash
uv sync
# or
pip install netmiko paramiko ansible-core
```

## Running the playbook

```bash
# Default command ("show version") against all devices
ansible-playbook -i hosts.yml site.yml --ask-vault-pass

# Custom command
ansible-playbook -i hosts.yml site.yml --ask-vault-pass -e remote_command="show ip interface brief"

# Login-test only (no command)
ansible-playbook -i hosts.yml site.yml --ask-vault-pass -e remote_command=""

# Control parallelism (default 20)
ansible-playbook -i hosts.yml site.yml --ask-vault-pass -e batch_size=5
```

## Vault management

```bash
# Encrypt secrets file (required before committing)
ansible-vault encrypt files/secrets.yml

# Edit an encrypted secrets file
ansible-vault edit files/secrets.yml
```

## Architecture

The project is flat (no subdirectory structure enforced at the repo root) with these key files:

| File | Role |
|---|---|
| `site.yml` | Main playbook — two plays: one running `multicred_login` role across `network_devices`, then a `localhost` play that merges JSON fragments into the CSV |
| `multicred_connect.py` | Custom Ansible module — drives Netmiko directly (bypasses `network_cli`) to attempt credential/protocol combinations at runtime |
| `hosts.yml` | Inventory — devices under `network_devices` group; per-host `device_platform` selects the Netmiko driver family |
| `all.yml` | Group vars — global defaults: `login_protocols`, `remote_command`, `login_timeout` |
| `credentials.yml` | Ordered credential list (labels + vault variable references); first entry is tried first |
| `secrets.yml` | Actual passwords (vault variables referenced by `credentials.yml`); **must be encrypted** |
| `main.yml` | Role tasks — calls the module, builds a result dict, writes a per-host JSON fragment to `output/.fragments/` |

### Key design decisions

**Why a custom module instead of `network_cli`?** Ansible's `network_cli` connection plugin requires the protocol and credential to be fixed before the play starts. The custom `multicred_connect` module drives Netmiko directly, enabling the runtime cycling loop.

**Why per-host JSON fragments?** `set_fact` values are not reliably shared across hosts running in parallel forks. Each host writes `output/.fragments/<hostname>.json`; the final localhost play merges them into the CSV, avoiding race conditions.

**Credential/protocol iteration order:** Outer loop = credentials (by list position), inner loop = protocols. So credential #1 over SSH is tried first, then credential #1 over Telnet, then credential #2 over SSH, etc.

**`device_platform` values** map to Netmiko driver families: `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_asa`, `cisco_xr`. The module appends `_telnet` automatically for Telnet connections.

### Per-host overrides

Override global vars on individual inventory hosts:
- `login_protocols` — restrict a device to `[telnet]` only
- `device_port` — non-standard SSH/Telnet port
