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
7. Sync the latest approved SPM export into a local IoT signature knowledge base.
8. Check SPM/Z-Intel IoT signature coverage and annotate local KB/family matches.
9. Produce an Excel review file sorted by hit volume so high-impact devices are reviewed first.
10. Upload the final Excel report to Google Drive and include the link in the completion email.

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
tools/
  spm_export_fetcher.py    # fetch latest approved SPM export and build local KB
  sig_family_report.py     # inspect grouped SPM UA signature families
utils/
  config.py, db.py, google_auth.py, logger.py, notifier.py, spm_kb.py, ua_normalizer.py
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

spm_export:
  enabled: true
  auto_sync: true
  export_id: latest
  # optional if different from spm.url/spm.api_key:
  # api_url: https://z-intel-plus.corp.zscaler.com/
  # api_key: <zintel-api-key>

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

### Progressive daily scheduler

For unattended daily operation, use the scheduler commands instead of manually
submitting one date at a time. The scheduler keeps a cursor in SQLite, polls and
processes active runs, and submits only the next missing eligible day. This is
safe to run from cron because it uses a lock file under `logs_dir`.

Enable and configure it in `config/settings.local.yaml`:

```yaml
scheduler:
  enabled: true
  name: daily_build_only
  query_name: build_only
  date_lag_days: 1              # do not submit today's still-changing logs
  max_active_rundeck_runs: 1    # keep only one long-running Rundeck job active
  auto_process_succeeded: true  # download/convert/filter/DeviceAtlas/SPM/report automatically
```

Initialize the first date once:

```bash
python run.py scheduler-set-base --date 2026-01-01 --query build_only
python run.py scheduler-status
python run.py scheduler-tick --dry-run
```

Then run one scheduler tick periodically from cron:

```cron
*/15 * * * * cd /mnt/ext_storage/iotSPM && . .venv/bin/activate && python run.py scheduler-tick >> logs/scheduler-cron.log 2>&1
```

Each tick follows this order:

1. process one queued retry, if any;
2. poll active Rundeck executions;
3. automatically process succeeded runs when `auto_process_succeeded: true`;
4. submit the next date from `base_date` up to `today - date_lag_days`, while
   respecting `max_active_rundeck_runs`.

If a processing run fails and you want cron to retry it later:

```bash
python run.py retry-add <run_id> --from-stage spm --note "SPM timeout"
python run.py retry-list
```

When `--from-stage` is omitted, the scheduler infers the next stage from the
run's stored `last_stage`.

### Process a completed run

```bash
python run.py process <run_id>
```

For long stages like DeviceAtlas mapping and SPM checks, add `--verbose` to see
stage start/done messages and throttled progress bars:

```bash
python run.py process <run_id> --from-stage spm --verbose
```

The processor is resumable. Each stage records its artifact path in SQLite and
will reuse that file on the next run unless you force a rebuild.

SPM/Z-Intel checks are also resilient inside the stage: transient `429/5xx`
responses are retried per User-Agent, completed rows are streamed to a
`.partial` CSV next to the SPM report, and a later `--from-stage spm` run resumes
from that partial file. If only a few UAs still fail after retries, they are
included as `spm-error` rows so the high-volume review report can still be built.

Before Stage 6, the orchestrator runs a lightweight SPM export sync. With
`spm_export.export_id: latest`, it checks the latest approved export id and only
downloads/rebuilds the local KB when that id changes. If sync fails and
`spm_export.required: false`, the pipeline logs a warning and continues with the
live SPM API check.

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

### SPM export KB and signature-family analysis

The local SPM KB is built from `phrases_req_uri.spm` inside an approved SPM
export. It keeps IoT UA patterns, groups related model patterns into families
using model-aware prefixes such as `mc9`, `pico`, `vr`, etc., and lets Stage 6
annotate candidate UAs before/alongside the live SPM API result.

Manual commands:

```bash
# Fetch latest approved export and rebuild KB only if export_id changed
python tools/spm_export_fetcher.py

# Force rebuild from latest approved export
python tools/spm_export_fetcher.py --force

# Build KB from an already extracted phrases_req_uri.spm file
python tools/spm_export_fetcher.py --local-file /path/to/phrases_req_uri.spm --export-id manual-2026-05

# Show largest grouped signature families
python tools/sig_family_report.py --limit 50

# Export family inventory to CSV for review
python tools/sig_family_report.py --csv data/reports/spm_families.csv
```

The Stage 6 CSV now includes these KB columns:

- `kb_match`
- `kb_refid`
- `kb_device_type`
- `kb_pattern`
- `kb_family`
- `kb_family_size`
- `kb_export_id`

The Excel report shows KB match/family columns and a consolidation note. If a UA
is marked `not-present` by the live SPM API but matches the local export KB, the
action becomes `review-existing-kb-match` so you can investigate mismatched
coverage/export freshness before adding a duplicate signature.

Failure emails include the last completed stage, error message, and a suggested
restart command. `python run.py status --limit 20` also shows `last_stage` and
the final report path when available.

### State reference / where to restart

Use the `state` shown by `python run.py status --limit 20` to decide the next
safe restart point:

```text
FILTERED              -> next stage is deviceatlas
DEVICEATLAS_ENRICHED  -> next stage is spm
SPM_CHECKED           -> next stage is report
REPORTED              -> next stage is upload/completed
COMPLETED             -> done
RUNDECK_FAILED        -> Rundeck failed, usually cannot process
FAILED                -> failed somewhere, check error/logs
```

Example:

```bash
# If state=DEVICEATLAS_ENRICHED, continue with SPM and later stages
python run.py process <run_id> --from-stage spm
```

### Process a local `.current` or `.csv` file

```bash
python run.py run-local /path/to/report.current --day 2026-01-01
python run.py run-local /path/to/report.csv --day 2026-01-01
python run.py run-local /path/to/report.csv --day 2026-01-01 --stop-after filter
```

### Google Drive upload of the final Excel report

Final XLSX reports are stored locally under `data/reports/` and, by default,
uploaded to Google Drive before the completion email is sent. The email includes
the uploaded report link when upload succeeds.

The same OAuth client/token used for Rundeck report download is reused for final
report upload. Keep `drive.readonly` for downloading Rundeck-generated report
files by id, and add `drive.file` for uploading final reports created by this
pipeline:

```yaml
google_drive:
  scopes:
    - https://www.googleapis.com/auth/drive.readonly
    - https://www.googleapis.com/auth/drive.file

report_upload:
  enabled: true
  folder_id: <optional-drive-folder-id>
```

If you previously authenticated with `drive.readonly`, re-run OAuth once because
the token scope changed:

```bash
python run.py auth-drive
```

If upload is not desired in a particular environment, disable it in
`config/settings.local.yaml`:

```yaml
report_upload:
  enabled: false
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
data/spm_exports/ # downloaded/extracted SPM exports when enabled
data/spm_knowledge_base.json # local IoT SPM UA pattern KB
db/             # SQLite state/cache
logs/           # iotspm.log
```

The final Excel report contains:

- Priority order by total hits
- Hardware type/vendor/model/marketing name
- SPM status: `detected-released`, `detected-reviewed`, `detected-disabled`, `not-present`
- Local SPM export KB match/family fields for duplicate/consolidation review
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
