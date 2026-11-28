# GreenLake and Compute Ops Management API endpoints

The public HPE GreenLake and Compute Ops Management (COM) REST endpoints that proliant-mcp reads, and what each one
returns. Everything here is read-only (`GET`). The behaviour notes were checked against a live workspace on 2026-11-28;
retention windows and limits can differ per workspace and change over time. IDs below are examples.

Official documentation: [COM developer guide](https://developer.greenlake.hpe.com/docs/greenlake/services/compute-ops-mgmt/public/guide),
[device management API](https://developer.greenlake.hpe.com/docs/greenlake/services/device-management/public).

## Hosts and sign-in

| What | URL |
|---|---|
| Token (client credentials) | `POST https://sso.common.cloud.hpe.com/as/token.oauth2`, form `grant_type=client_credentials`, HTTP Basic with the API client's ID and secret. The bearer token lasts about 2 hours |
| COM API, by region | `https://us-west.api.greenlake.hpe.com`, `https://eu-central.api.greenlake.hpe.com`, `https://ap-northeast.api.greenlake.hpe.com` |
| GreenLake platform API (all regions) | `https://global.api.greenlake.hpe.com` |

- The same token worked on the COM host and on the global host. An API client created for Compute Ops Management only may be
  refused (HTTP 401 or 403) on the global host; one created for the whole workspace works on both.
- A request to a region where COM is not set up for the workspace answers HTTP 404 with "routing details". proliant-mcp then
  tries the other regions.
- Lists page with `limit` and `offset`. COM list endpoints accepted `limit=500`, the audit log pages of 100 and the device
  list `limit=200`.

## Compute Ops Management (regional host)

| Endpoint | Returns | Checked |
|---|---|---|
| `GET /compute-ops-mgmt/v1/servers` | Every server: health, power, connection, subscription, firmware inventory, `createdAt` (when COM first got it), `deviceClaimType` | Yes |
| `GET /compute-ops-mgmt/v1/servers/{id}` | One server in full, including the last firmware update and host OS | Yes |
| `GET /compute-ops-mgmt/v1/servers/{id}/inventory` | Hardware inventory: CPUs, GPUs, memory, drives, NICs, PCI devices | Yes |
| `GET /compute-ops-mgmt/v1/alerts`, `/servers/{id}/alerts` | Alerts with severity and cleared state | Yes |
| `GET /compute-ops-mgmt/v1/jobs` | Jobs (firmware updates, inventory collection, power). No creator field. About 3 months kept. A `filter` on the resource answered HTTP 400 | Yes |
| `GET /compute-ops-mgmt/v1/groups`, `/firmware-bundles` | Server groups; firmware bundles | Yes |
| `GET /compute-ops-mgmt/v1beta2/activities` | Event log per server (connected, disconnected, power off, health, firmware, settings). About 3 months kept. A few messages name the user, with part of the address hidden. `/v1/activities` answers 404 | Yes |
| `GET /compute-ops-mgmt/v1beta1/utilization-by-entity?start-date&end-date&metric-type=CPU_UTILIZATION` | One high, low and average per server for the period, and `excluded`, the number of servers with no data. Add `resource-uri` for one server | Yes |
| `GET /compute-ops-mgmt/v1beta1/utilization-over-time?start-date=2026-10-29&end-date=2026-11-28&sample-size=1_DAY&metric-type=CPU_UTILIZATION&resource-uri=/compute-ops-mgmt/v1/servers/{id}` | One server's average per day (`intervalsCollected`; today's point is `null`) | Yes |
| `GET /compute-ops-mgmt/v1beta1/energy-by-entity?start-date&end-date` | kWh, CO2e and cost per server (`min`, `max`, `average`, `percents`), the fleet sums and `excluded`. `status: PARTIAL` means only part of the period has data | Yes |
| `GET /compute-ops-mgmt/v1beta1/energy-over-time?start-date&end-date&sample-size=1_DAY&resource-uri=...` | One server's kWh, CO2e and cost per day, plus projected values | Yes |
| `GET /compute-ops-mgmt/v1beta2/reports` | The reports created in the workspace | Yes |
| `POST /compute-ops-mgmt/v1beta2/reports` | Creates a report. Only `CARBON_FOOTPRINT` and `SERVER_HARDWARE_INVENTORY` are accepted, and `CARBON_FOOTPRINT` answered "report generation is not supported" on the test workspace. proliant-mcp never sends it | Tried once |

Other `metric-type` values in the documentation, not tried: `MEMORY_BUS_UTILIZATION`, `IO_BUS_UTILIZATION`,
`CPU_INTERCONNECT_UTILIZATION`. The documentation also describes `sample-size=1_HOUR` with a UTC timestamp for `start-date`
and `end-date` instead of a date; not tried.

- **Server IDs** are the product ID and the serial number joined with `+`, for example `P12345-B21+CZ1234567`. In a path keep
  the `+`. In a query string value, such as `resource-uri`, write it as `%2B`.
- **Which API version:** `servers`, `jobs`, `alerts`, `groups` and `firmware-bundles` are under `v1`, the usage and energy
  endpoints under `v1beta1`, and `activities` and `reports` under `v1beta2`.
- **Retention:** usage and energy accept a start date up to 180 days back; an older one answers HTTP 400 "'start-date' should
  be within past 180 days from today". Activities and jobs went back about 3 months on the test workspace.
- **Coverage differs per metric.** On the test workspace COM had CPU data for 16 of 42 servers (Intel rack servers only) and
  energy data for 32 (every connected server except one); `excluded` counts the rest. Servers that are not connected have no
  data, AMD servers had energy but no CPU data, and Synergy blades had no CPU data.
- **Times** carry no time zone in the usage and energy answers; treat them as UTC.

## GreenLake platform API (global host)

| Endpoint | Returns | Checked |
|---|---|---|
| `GET /audit-log/v1/logs` | The workspace audit log: user, category, description, `additionalInfo.ipAddress`, time. Account and user events (sign-ins, API clients created or deleted, console launches), not per server. 80 entries over 11 days on the test workspace. `/audit-log/v1beta1/logs` answers 404 | Yes |
| `GET /devices/v1/devices` | The workspace device inventory (servers, switches, storage): serial, model, `createdAt`, `assignedState`, region, subscription tier and end. 43 devices on the test workspace | Yes |
| `GET /service-catalog/v1/service-managers` | The GreenLake service catalog (Compute Ops Management, Data Services, ...) | Yes |
| `GET /service-catalog/v1/service-manager-provisions` | Where each service is provisioned in the workspace, by region | Yes |

- **The audit log ignored date parameters.** `startTime` and `endTime` had no effect and a `filter` on `createdAt` answered
  HTTP 400, so proliant-mcp pages the whole log and filters it itself.
- **`createdAt` in the device inventory** is when the device record was created in the workspace inventory as far as the API
  shows. A server's COM record can be much newer, because it was assigned to Compute Ops Management later. Neither record has
  a creator field. Blades that join COM through OneView have no device record.
- **Retention is short.** The audit log held only the last 11 days on the test workspace, so an add or change from a year
  ago is not in it.

## Where proliant-mcp uses them

| Tool | Endpoints |
|---|---|
| `com_list_servers_table`, `com_get_overview`, `com_get_server` | `/servers`, `/servers/{id}`, `/servers/{id}/inventory`, `/alerts` |
| `com_list_alerts`, `com_list_jobs`, `com_list_groups`, `com_list_firmware_bundles` | `/alerts`, `/jobs`, `/groups`, `/firmware-bundles` |
| `com_server_usage` | `utilization-by-entity`, `energy-by-entity`, and for one server `utilization-over-time` |
| `com_list_activities` | `v1beta2/activities` |
| `greenlake_audit_log` | `audit-log/v1/logs` |
| `greenlake_list_devices` | `devices/v1/devices` |
| `greenlake_list_services` | `service-catalog/v1/service-managers` and `service-manager-provisions` |
| `com_api_get` | Any read-only `GET` under `/compute-ops-mgmt/`, for data no tool covers |

`com_api_get` accepts only paths that start with `/compute-ops-mgmt/`, so the global endpoints above are reachable only
through their own tools. Creating the API client at sign-in uses GreenLake web endpoints that are not public APIs and are not
listed here.
