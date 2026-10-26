# MDK network timeline

See where the time goes in an SAP Mobile Development Kit (MDK) app: the device, SAP Mobile Services,
the Cloud Connector or the SAP backend.

You give it the network trace Mobile Services records (`.har`) and, optionally, the MDK client log of
the same run (`clientlogs-*.json`). You get back one self-contained HTML page with:

- **Every call, split by component.** The SAP backend time comes from the `sap-perf-fesrec` header.
  Cloud Connector + network is the SAP-bound hop minus the backend time. Mobile Services is the first
  hop minus the SAP-bound hop. Mobile Services logs each call once per hop, and those hops are folded
  into one row.
- **Offline-sync windows.** It finds the MobiLink / sync-progress calls and the reads the offline
  server makes, and shows how much of each window is idle. Idle means no request is open at Mobile
  Services: the device is busy, or data is on its way to or from it.
- **Device log lines on the same clock.** The client log is lined up with the HAR using the
  "SUPPORT LOG SENT" line on the device and the log-upload request seen at Mobile Services.
- **An "action → data downloaded" breakdown.** It takes an online call that changes data (e.g. a
  POST that creates a record) and the offline download that follows it, and splits every millisecond
  between SAP, the Cloud Connector, Mobile Services and the device / network. It shows this as a
  time-ordered strip, totals per component, and a step-by-step table.
- **An end-to-end trace of one offline read**, from the device to SAP and back. Three IDs from the
  HAR link the hops:
  - `X-Correlationid`: the device's sync request, carried onto the backend reads it triggers.
  - `operationid`: the offline sync session.
  - The SAP Passport transaction ID: the same on every hop and recorded by the ABAP server.

  The trace also gives the payload size, whether delta tracking returned a delta link, and the
  values to look up in STAD, `/IWFND/GW_CLIENT`, SAT and ST05.

The HAR and client log never leave your machine unless you deploy the web app yourself.

## 1. Capture a trace

In the **SAP Mobile Services cockpit**, open your app:

1. **Network trace:** *Mobile Applications → your app → Mobile Network Trace* (the exact path differs
   between cockpit versions). Turn tracing on, run the scenario on the device, then download the trace
   as a `.har`.
2. **Client log (optional, recommended):** *Client Resources / Client Logs*. Set the log level to
   **Debug** for the user before the test; a re-logon on the device picks up the policy. After the run,
   tap **Send support log** (or call `SendSupportLogs`) in the app, then download the JSON from the
   cockpit.

The HAR holds request and response bodies, user names and IDs. Treat it as confidential.

## 2. Run it

You need Python 3.10 or later.

### Command line (no dependencies)

```bash
python timeline.py trace.har clientlogs.json -o timeline.html
```

Open `timeline.html` in a browser. Options:

| Option | Meaning |
|---|---|
| `clientlogs.json` | Optional second argument: the client log of the same run |
| `-o FILE` | Write the page to FILE (default: stdout) |
| `--title TEXT` | Page title |
| `--action PATTERN` | URL pattern (regex or plain text) of the online call to break down. Default: the first online POST / PATCH / PUT / DELETE that is followed by an offline sync within 15 s |
| `--download PATTERN` | URL pattern of the offline read(s) to trace end to end. Default: the slowest entity read (not `$metadata`) of each sync session |
| `--device-offset-ms N` | Device clock offset, if the client log has no "SUPPORT LOG SENT" marker |

Example: break down a `createOrder` action and follow the `Orders` download:

```bash
python timeline.py trace.har clientlogs.json --action createOrder --download "^/Orders" -o timeline.html
```

### Web app (upload form + saved traces)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python main.py                         # Windows: .venv\Scripts\python main.py
```

Open http://localhost:5001, upload the files, and tick **Save** to keep a trace under a title.
Saved traces are stored gzipped in `data/traces.db` (SQLite). They're re-analysed every time you open
them, so improvements to the parser also apply to old traces.

With no credentials set, the app is open on your own machine. To require a login, create `plash.env`
next to `main.py` (copy `plash.env.example`):

```bash
export APP_USER=someone
export APP_PWD=a-long-password
export SESSION_SECRET=a-long-random-string
export DISPLAY_TZ=Europe/Berlin      # optional: time zone for the saved-traces list (default UTC)
```

### Deploy (optional)

The web app is a [FastHTML](https://fastht.ml) app and runs as-is on [pla.sh](https://pla.sh):

```bash
pip install plash-cli
plash_login
plash_deploy --name my-network-timeline
```

When deployed, the app **refuses every login unless `plash.env` sets `APP_USER` and `APP_PWD`**, so
traces are never exposed by accident. `data/` survives redeploys; don't pass `--force_data`, which
overwrites it.

## Reading the result

- **Device + network** (hatched) is time when no request is open at Mobile Services. A HAR can't tell
  device work from network latency. Add a Debug client log to see what the device measured.
- App log lines written as `[Tag] something ... after 123 ms` are picked up as device-measured steps
  and compared with the Mobile Services times.
- Inside one call, the hops are assumed to be nested evenly. The per-call totals are exact; where
  each part sits within the call is approximate.

## Files

| File | What it does |
|---|---|
| `timeline.py` | Parsing, correlation and the timeline data; also the command line |
| `timeline_template.html` | The page; the data is inserted into it as JSON |
| `main.py` | FastHTML upload form, login and saved traces |

## License

MIT, see [LICENSE](LICENSE).
