# MPT hourly traffic monitoring

The Traffic Monitor page is intended for authorized PS, CS and VoLTE telemetry.
It does not generate or simulate live MPT network values. An approved MPT
telemetry service must submit one sample per hour to the backend.

## Start the backend

From `backend`, install `requirements.txt` and run:

```powershell
$env:API_KEY = "a-long-random-secret"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Replace the default API key before use and keep it out of source control,
URLs, browser storage and telemetry payloads. The dashboard is served at
`http://127.0.0.1:8000/`. Enter that address and the API key on the Traffic
Monitor page; the key stays in page memory and is cleared when the page reloads.

## Configure monitoring

1. Connect the dashboard to the backend.
2. Select the PS, CS and/or VoLTE services, set the approved fluctuation
   threshold, and choose the vendor/workflow key.
3. Enable hourly monitoring. Enable automatic job queueing separately only
   after the threshold and workflow have been approved.

The first sample from a telemetry source establishes its baseline. Subsequent
samples are compared with the previous sample from the same source. Both
increases and decreases at or above the configured percentage threshold create
one queued job per affected service and telemetry event. Re-submitting an event
with the same `source_event_id` is idempotent. Samples must arrive in timestamp
order for each source and use consistent units.

## Telemetry request

Send an authenticated `POST /api/traffic/samples` with `Content-Type:
application/json` and the `X-API-Key` header:

```json
{
  "source_event_id": "unique-hourly-event-id",
  "source": "approved-mpt-aggregator",
  "sampled_at": "2026-10-16T09:00:00+06:30",
  "ps": 12500,
  "cs": 3400,
  "volte": 780
}
```

Use the actual agreed traffic units and an ISO-8601 timestamp with a timezone.
The source identifier should remain stable so readings are compared against
the correct baseline. Duplicate event IDs return the stored sample without
creating additional jobs. Stale readings are rejected with HTTP 409.

`GET /api/traffic` returns the latest readings, percentage changes and recent
history. `GET /api/traffic/config` and `PUT /api/traffic/config` read and update
the saved monitoring policy. These endpoints, telemetry ingestion and job
management require `X-API-Key`.

## Robot execution limitation

Traffic thresholds enqueue records in `/api/jobs`; they do not execute a
desktop robot. This workspace has no AISWARE or Cyclone execution adapter,
credential vault, MPT telemetry connector or production approval workflow.
Until those components are integrated and validated, use the simulator for
testing and treat AISWARE/Cyclone jobs as queued requests only. Do not enable
production auto-run until MPT has approved the source, units, trigger policy,
workflow mapping, access controls and operational support.
