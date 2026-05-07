# iotSPM

Automated pipeline for prioritizing IoT User-Agent signatures from Zscaler logs using DeviceAtlas enrichment and Z-Intel/SPM coverage checks.

## Goal

The pipeline turns large Zscaler UA log exports into a small, prioritized review report:

1. Submit/poll long-running Rundeck Zscaler queries.
2. Download generated reports from Google Drive.
3. Convert `.current` files to CSV using `zsclient` / `zclient`.
4. Clean junk/non-IoT UAs and deduplicate version/build variants.
5. Enrich remaining UAs with DeviceAtlas device properties.
6. Drop obvious mobile phone/desktop/tablet traffic.
7. Check SPM/Z-Intel IoT signature coverage.
8. Produce an Excel review file sorted by hit volume so high-impact devices are reviewed first.

## Repository layout

```text
config/
  settings.yaml            # main configurable defaults
  settings.local.yaml      # optional private overrides, gitignored
  iot_device_types.yaml    # DeviceAtlas hardware types to keep/reject
  ua_blocklist.yaml        # regex filters for junk/desktop/bot traffic
pipeline/
  stage1_rundeck.py        # submit/poll Rundeck query
  stage2_download.py       # Google Drive download
  stage3_convert.py        # .current -> .csv conversion
  stage4_filter.py         # UA cleaning and dedupe/grouping
  stage5_deviceatlas.py    # DeviceAtlas enrichment/cache
  stage6_spm.py            # SPM/Z-Intel coverage check/cache
  stage7_report.py         # XLSX review report
utils/
  config.py, db.py, google_auth.py, logger.py, notifier.py, ua_normalizer.py
orchestrator.py            # stateful pipeline orchestration
run.py                     # CLI entry point
```

## Server setup

Target server path assumed by default:

```bash
/mnt/ext_storage/iotSPM
```

Clone/copy the repo there, then create a virtual environment:

```bash
cd /mnt/ext_storage/iotSPM
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Configure DeviceAtlas Enterprise Python API

Place/extract the package so this exists:

```text
/mnt/ext_storage/iotSPM/Deviceatlas/deviceatlas-enterprise-python-3.2.1/API/deviceatlas-enterprise-3.2.1/src/com/deviceatlas/device
```

You do **not** need to run `setup.py install`, and you should avoid `sudo` for
venv-based runs. `sudo setup.py install` installs into the system Python, but
the pipeline runs inside `.venv`, so `.venv` will not see that package.

Instead, point `paths.deviceatlas_python_api_dir` to either the extracted `API`
directory or directly to the nested `src` directory in `config/settings.local.yaml`:

```bash
cp config/settings.yaml config/settings.local.yaml
vim config/settings.local.yaml
```

Example override:

```yaml
paths:
  deviceatlas_python_api_dir: /mnt/ext_storage/iotSPM/Deviceatlas/deviceatlas-enterprise-python-3.2.1/API
  # This also works:
  # deviceatlas_python_api_dir: /mnt/ext_storage/iotSPM/Deviceatlas/deviceatlas-enterprise-python-3.2.1/API/deviceatlas-enterprise-3.2.1/src
```

The loader auto-discovers common extracted layouts and adds the correct import
root to `sys.path`, including the directory above `com/deviceatlas`.

Also place the DeviceAtlas data file at the path configured in `config/settings.yaml`:

```text
/mnt/ext_storage/iotSPM/DeviceAtlas.json
```

If your file/path differs, set it in `config/settings.local.yaml`.

## Private configuration

Create a local override file:

```bash
cp config/settings.yaml config/settings.local.yaml
```

Edit at least:

```yaml
rundeck:
  base_url: https://<your-rundeck-host>
  project: <rundeck-project>
  username: <username>
  password: <password>
  upload: GoogleDrive
  query_type: web_query
  allow_parallel_runs: false
  job_ids:
    build_only: <working-rundeck-job-uuid-for-this-query>
    all_location: <working-rundeck-job-uuid-for-this-query>
    enriched_location: <working-rundeck-job-uuid-for-this-query>

spm:
  api_key: <zintel-api-key>

smtp:
  enabled: true
  host: <smtp-host>
  username: <smtp-user>
  password: <smtp-password>
  alert_email_from: <from>
  alert_email_to:
    - <you@example.com>
```

Environment variables can override common secrets:

```bash
export RUNDECK_USERNAME='...'
export RUNDECK_PASSWORD='...'
export ZINTEL_API_KEY='...'
```

The Rundeck submitter reads the live job page and dynamically maps option names before posting to `/project/<project>/job/index`. If your Rundeck job uses unusual option names, add an `option_names` override in `config/settings.local.yaml`, for example:

```yaml
rundeck:
  option_names:
    query: extra.option.Query
    start_time: extra.option.StartTime
    end_time: extra.option.EndTime
    output: extra.option.Output
    upload: extra.option.Upload
    cloud: extra.option.Cloud
```

If submit fails with `HTTP 404` on `/project/<project>/job/show/<job_id>`, the query job UUID or project/base URL is wrong for your environment. Open the Rundeck job manually in a browser and copy the UUID from the working job URL into `rundeck.job_ids.<query_name>` in `config/settings.local.yaml`.

## Google Drive OAuth

Put the OAuth client secret file here:

```text
/mnt/ext_storage/iotSPM/credentials/credentials.json
```

Run one-time auth:

```bash
python run.py auth-drive
```

It will print a URL/code flow for headless server auth. Open the URL manually in your browser, approve access, then paste either the returned code or the full `http://localhost:8080/?code=...` redirected URL into the terminal.

After successful auth it saves:

```text
/mnt/ext_storage/iotSPM/credentials/token.json
```

Future runs reuse `token.json` silently. You should not need to authenticate again unless `token.json` is deleted, the refresh token is revoked, or Google/client credentials change.

Seeing Chrome show `This site can't be reached` for `http://localhost:8080/...` is expected in this manual server flow. The important part is the `code=...` in the address bar. Paste that full URL into the terminal; the server exchanges the code and verifies Drive access before saving `token.json`.

## Common commands

### Submit a query

```bash
python run.py submit --day 2026-01-01 --query build_only
```

This prints a `run_id` and stores Rundeck execution state in SQLite.

### Poll until complete

```bash
python run.py poll <run_id>
```

Run from cron every few minutes if the report can take hours/days:

```cron
*/10 * * * * cd /mnt/ext_storage/iotSPM && . .venv/bin/activate && python run.py poll <run_id>
```

For fully automated tracking of all submitted jobs, use:

```cron
*/10 * * * * cd /mnt/ext_storage/iotSPM && . .venv/bin/activate && python run.py poll-active --auto-process
```

When status becomes `succeeded`, the pipeline stores the Google Drive file id if it can parse it from the Rundeck output.

### Process a completed run

```bash
python run.py process <run_id>
```

The processor is resumable. Each stage records its artifact path in SQLite and
will reuse that file on the next run unless you force a rebuild.

```bash
# Rebuild SPM, report, and upload only; reuse raw/csv/cleaned/DeviceAtlas files
python run.py process <run_id> --from-stage spm

# Rebuild only the Excel report from the existing SPM CSV
python run.py process <run_id> --force-stage report

# Rebuild everything from the downloaded/raw file onward
python run.py process <run_id> --from-stage convert

# Debug/review an intermediate artifact and stop
python run.py process <run_id> --stop-after deviceatlas
```

Failure emails include the last completed stage, error message, and a suggested
restart command. `python run.py status --limit 20` also shows `last_stage` and
the final report path when available.

### Process a local `.current` or `.csv` file

```bash
python run.py run-local /path/to/report.current --day 2026-01-01
python run.py run-local /path/to/report.csv --day 2026-01-01
python run.py run-local /path/to/report.csv --day 2026-01-01 --stop-after filter
```

### Optional upload of the final Excel report

By default final XLSX reports are stored locally under `data/reports/`. To also
upload them to Google Drive, enable this in `config/settings.local.yaml` and use
a write-capable Drive scope:

```yaml
google_drive:
  scopes:
    - https://www.googleapis.com/auth/drive.file

report_upload:
  enabled: true
  folder_id: <optional-drive-folder-id>
```

Then re-run OAuth once because the token scope changed:

```bash
python run.py auth-drive
```

### Show latest runs

```bash
python run.py status --limit 20
```

## Output

Generated files go under:

```text
data/raw/       # downloaded .current/.csv
data/csv/       # converted CSV
data/cleaned/   # filtered/deduped UA groups
data/enriched/  # DeviceAtlas enrichment
data/reports/   # SPM CSV + XLSX review report
db/             # SQLite state/cache
logs/           # iotspm.log
```

The final Excel report contains:

- Priority order by total hits
- Hardware type/vendor/model/marketing name
- SPM status: `detected-released`, `detected-reviewed`, `detected-disabled`, `not-present`
- Suggested action
- Original User-Agent

## Filtering / dedupe strategy

The pipeline intentionally avoids deleting too aggressively before DeviceAtlas. It only removes obvious junk, bots, exploit strings, and common desktop/mobile browser UAs. Then it groups near-duplicate UAs using:

- app family
- Android version
- model/device token
- normalized build prefix

Within each group it keeps the highest-hit UA as the representative and records total group hits + group size. This targets high-volume IoT patterns instead of one-off version/build variants.

Tune these files as new cases appear:

```text
config/ua_blocklist.yaml
config/iot_device_types.yaml
```

## Notes / known integration points

- `pipeline/stage1_rundeck.py` may need minor HTML parsing adjustments depending on the exact Rundeck result page for your environment.
- `pipeline/stage3_convert.py` assumes command form: `zclient -o output.csv -rc input.current`. If your installed binary uses a different syntax, update that function.
- `pipeline/stage5_deviceatlas.py` supports both installed DeviceAtlas API and `sys.path` fallback to `deviceatlas_python_api_dir`.
- Signature creation/upload is intentionally not automated yet. This version produces the review input required before adding signatures.
