---
name: hpe-compute-fleet
description: "Report on HPE server fleets through the proliant-mcp Compute Ops Management (com_*) and OneView (oneview_*) tools: fleet health, servers needing attention, disconnected servers, subscriptions, active alerts and corrective actions, jobs, server groups and compliance, firmware bundles and baselines, Synergy frames, profiles and interconnects. Use for questions like 'how healthy is my fleet', 'which servers are critical', 'any active OneView alerts', 'what SPPs are available', 'is server X in COM or OneView', or a cross-source health or firmware report."
---

# HPE compute fleet (COM + OneView)

Live, read-only data from HPE Compute Ops Management (GreenLake cloud,
`com_*` tools) and HPE OneView / Synergy Composer (`oneview_*` tools) through
the `proliant-mcp` MCP server. For one server's deep detail through its
iLO (DIMMs, drives, NIC LLDP, IML), use the `proliant-ilo` skill.

Call `list_servers` first. It returns `oneview_appliances` and `com_accounts`.
Pass `appliance` / `account` only when more than one is configured.

## Pick the tool

| Question | Tool |
|---|---|
| Fleet health report (counts, needs attention, disconnected, alerts) | `com_get_overview` |
| Find or filter COM servers (health, connected, name) | `com_list_servers` |
| One COM server: subsystem health, firmware, last update, its alerts | `com_get_server` (name, serial, iLO IP or id) |
| COM alerts, newest first | `com_list_alerts` (`server`, `severity`, `include_cleared`) |
| What COM did recently (firmware updates, inventory, OneView sync) | `com_list_jobs` (`state`) |
| Server groups and compliance | `com_list_groups` |
| Available SPPs / patches in COM | `com_list_firmware_bundles` |
| Anything else in COM (server inventory, settings, appliances) | `com_api_get`, e.g. `/compute-ops-mgmt/v1/servers/{id}/inventory` |
| OneView appliance version, health, counts, alert totals | `oneview_get_appliance` |
| OneView servers (status, profile, bay, iLO) | `oneview_list_server_hardware` |
| One OneView server: firmware components, its alerts | `oneview_get_server_hardware` |
| Profiles, templates, compliance, firmware baseline | `oneview_list_server_profiles` |
| OneView alerts with corrective action | `oneview_list_alerts` (`active_only`, `severity`) |
| Synergy frames: power, PSUs, fans, Composers | `oneview_list_enclosures` |
| Virtual Connect / interconnect modules | `oneview_list_interconnects` |
| SPPs / Synergy Service Packs on OneView | `oneview_list_firmware_baselines` |
| Anything else in OneView (networks, uplink sets, logical enclosures, tasks) | `oneview_rest_get`, e.g. `/rest/ethernet-networks` |

## Workflows

**Fleet health report:** `com_get_overview` and `oneview_get_appliance` in
parallel. Report totals, then a table of `needs_attention` servers. For each
critical or warning server, `com_get_server` and quote `health_detail`
(which subsystem) and its newest active alert with `resolution`. Add the
OneView active alert count and the top alerts from `oneview_list_alerts`.

**Alert triage:** `com_list_alerts(severity="CRITICAL")`, then `"WARNING"`;
group by `server` and `category`. For OneView, `oneview_list_alerts` and quote
`corrective_action`. Ignore cleared alerts unless asked for history.

**Firmware drift:** COM: `com_list_firmware_bundles` for the newest bundle per
`generation`, then `com_list_servers` and compare `firmware_bundle_id`; check
`com_list_groups` for `firmware_compliance`. OneView:
`oneview_list_firmware_baselines` for the newest Service Pack, then
`oneview_list_server_profiles` and compare `firmware_baseline`.

**Find one server everywhere:** match by serial number. `com_get_server`
(serial) shows `connection_type` (`ONEVIEW` means COM sees it through a OneView
appliance) and the iLO IP; `oneview_get_server_hardware` (serial) gives its
bay and profile; if the iLO is in `list_servers`, the iLO tools give the
deepest detail.

## Rules

- COM `health` values are upper case (`OK`, `WARNING`, `CRITICAL`); OneView
  `status` is `OK` / `Warning` / `Critical`.
- `connected: false` in COM means the iLO isn't talking to COM right now, so
  COM's health and power data may be stale. Say so.
- Report tool errors as they come back (login failure, 403 = the API client has
  no COM role). Never fill in values from memory.
- All tools are read-only. Changes (power, firmware updates, profile edits)
  are not available in this version; don't suggest the tools can do them.
