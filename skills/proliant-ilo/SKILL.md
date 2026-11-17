---
name: proliant-ilo
description: "Read HPE ProLiant server state through the hpe-compute-mcp iLO tools: identity, power/POST state, health, iLO version and license, firmware, CPUs/GPUs, memory, storage, NICs with LLDP neighbors, and IML/iLO event logs. Use for questions like 'what iLO version is on X', 'which servers have GPUs', 'is the server healthy', 'what switch port is this NIC on', 'show recent hardware errors', or a fleet firmware audit."
---

# ProLiant iLO (hpe-compute-mcp)

All data comes live from each server's iLO Redfish API through the
`hpe-compute-mcp` MCP server. Every tool is read-only. For fleet-wide
questions through Compute Ops Management or OneView, use the
`hpe-compute-fleet` skill.

## Pick the tool

| Question | Tool |
|---|---|
| Which servers exist? | `list_servers` (always call first; use the exact names from `servers`) |
| Model, serial, power, POST, health, UID, BIOS/iLO version, CPU/memory totals | `get_server_summary` |
| Same, for every server at once | `get_fleet_summary` |
| iLO generation, firmware and date, license, COM connection, iLO IP/MAC/DHCP | `get_ilo_info` |
| Per-subsystem health, fan / PSU redundancy | `get_health` |
| Every firmware component version | `get_firmware_inventory` |
| CPUs and GPUs | `get_processors` |
| DIMMs, populated/empty slots | `get_memory` |
| Controllers, drives, RAID volumes | `get_storage` |
| Host NICs, MACs, link, switch/port via LLDP | `get_network_adapters` |
| Hardware events / iLO events | `get_event_log` (`log="iml"` or `"iel"`, `severity`) |
| Anything else (BIOS attributes, boot order, thermal, power) | `redfish_get` with a `/redfish/v1/...` path |

## Workflows

**Firmware audit across a fleet:** `get_fleet_summary` for iLO/BIOS versions,
then `get_firmware_inventory` per server for NIC/storage/CPLD. Present a
table: rows = component, columns = servers; call out mismatches.

**Health triage:** `get_health` -> for any subsystem not `OK`, read
`get_event_log` with `severity="Critical"` then `"Warning"` and quote the
newest messages with timestamps. Check `repaired` before calling an event
current.

**Which switch port is this server on?** `get_network_adapters`: each port's
`lldp_neighbor` gives the switch (`chassis_id`, `management_ipv4`) and
`port`. LLDP must be enabled on the switch; a null neighbor means none heard.

**Useful `redfish_get` paths** (iLO 5/6/7):
- `/redfish/v1/Systems/1/Bios` - current BIOS attributes (pending changes: `/Bios/Settings`)
- `/redfish/v1/Systems/1/BootOptions?$expand=.` - boot options
- `/redfish/v1/Chassis/1/Thermal`, `/redfish/v1/Chassis/1/Power` - temps, fans, PSU watts
- `/redfish/v1/Managers/1/SecurityService` - iLO security state

## Rules

- Report errors per server (unreachable iLO, 401 login failure) with the
  server name; never fill in values from memory.
- iLO 6 reports `FirmwareVersion` like `iLO 6 v1.74`; iLO 7 like `1.21.00`.
  Use `ilo_version` from the tools, which normalizes both.
- `part_number: null` means iLO did not report one, not that it's missing.
- Changes (power, UID, BIOS, iLO network, firmware) are not available in this
  version of the server. Don't suggest the tools can do them.
