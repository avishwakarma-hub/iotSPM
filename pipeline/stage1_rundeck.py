from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
from bs4 import BeautifulSoup


TIME_FORMAT = "%d%b%Y:%H:%M:%S"
TERMINAL_STATES = {"succeeded", "failed", "aborted", "timedout"}
RUNNING_STATES = {"running", "scheduled"}
DRIVE_LINK_RE = re.compile(r"https://drive\.google\.com/[^\s'\"<>]+")


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


class RundeckClient:
    """Rundeck UI/API client used by the iotSPM ingestion pipeline.

    This intentionally mirrors the proven workflow from the existing
    `invoke_rundeck_query.py` helper: authenticate through the Rundeck UI,
    read the job page form/CSRF token, submit to `/job/index`, and use the
    execution-output API for status and Drive-link discovery.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg["rundeck"]
        self.cfg["base_url"] = self.cfg["base_url"].rstrip("/")
        self.session = requests.Session()
        self.session.verify = bool(self.cfg.get("verify_ssl", False))
        if not self.session.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session_id: Optional[str] = None

    def login(self) -> None:
        url = f"{self.cfg['base_url']}/j_security_check"
        cookies = {"communityNews": "20"}
        if self.cfg.get("old_sessionid"):
            cookies["JSESSIONID"] = self.cfg["old_sessionid"]

        resp = self.session.post(
            url,
            headers={
                "Origin": self.cfg["base_url"],
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{self.cfg['base_url']}/user/login",
                "User-Agent": self._ua(),
            },
            cookies=cookies,
            data={"j_username": self.cfg["username"], "j_password": self.cfg["password"]},
        )
        self._raise_for_status(resp, "login to Rundeck", url)
        self.session_id = self.session.cookies.get("JSESSIONID")
        if not self.session_id:
            raise RuntimeError("Rundeck login failed: JSESSIONID cookie missing")

    def submit_query(
        self,
        query: str,
        start_time: str,
        end_time: str,
        output_format: Optional[str] = None,
        query_name: Optional[str] = None,
    ) -> str:
        self._ensure_login()
        job_id = self._job_id_for_query(query, query_name)

        running_jobs = self._get_user_running_jobs()
        if running_jobs and not bool(self.cfg.get("allow_parallel_runs", False)):
            job = running_jobs[0]
            raise RuntimeError(
                f"Rundeck already has a running job for {self.cfg.get('username')}: "
                f"execution #{job.get('execution_id')} ({job.get('job_type')}) "
                f"status={job.get('status')} query={job.get('query', '')[:120]}"
            )

        page = self._job_page_context(job_id)
        form_data = self._build_run_form_data(
            job_id=job_id,
            query=query,
            start_time=start_time,
            end_time=end_time,
            output_format=(output_format or self.cfg.get("output_format", "CSV")),
            page=page,
        )
        run_url = f"{self.cfg['base_url']}/project/{self.cfg['project']}/job/index"
        resp = self.session.post(
            run_url,
            headers={
                "Referer": self._job_show_url(job_id),
                "Origin": self.cfg["base_url"],
                "User-Agent": self._ua(),
            },
            cookies=self._cookies(),
            data=form_data,
        )
        self._raise_for_status(resp, "submit Rundeck query", run_url, job_id)

        execution_id = self._extract_execution_id_from_submit_response(resp.text)
        if not execution_id:
            snippet = re.sub(r"\s+", " ", resp.text[:800]).strip()
            raise RuntimeError(
                "Query submitted but execution id could not be extracted from Rundeck response. "
                f"Response snippet={snippet!r}"
            )
        return execution_id

    def poll_execution(self, execution_id: str) -> Dict[str, Any]:
        self._ensure_login()
        output = self._get_execution_output(execution_id, offset=0, maxlines=5)
        status = self._normalize_state(output.get("execState", "running"))
        completed = bool(output.get("execCompleted", False))

        drive_link: Optional[str] = None
        drive_file_id: Optional[str] = None
        drive_file_name: Optional[str] = None
        if completed:
            drive_link = self._extract_drive_link_from_logs(execution_id)
            drive_file_id = self._extract_drive_file_id(drive_link) if drive_link else None
            drive_file_name = self._extract_drive_file_name_from_link(drive_link) if drive_link else None

        return {
            "execution_id": execution_id,
            "status": status,
            "completed": completed,
            "drive_link": drive_link,
            "drive_file_id": drive_file_id,
            "drive_file_name": drive_file_name,
            "url": self._execution_show_url(execution_id),
            "duration_ms": output.get("execDuration", 0),
        }

    def wait_for_completion(self, execution_id: str, callback=None) -> Dict[str, Any]:
        interval = int(self.cfg.get("poll_interval_seconds", 300))
        while True:
            result = self.poll_execution(execution_id)
            if callback:
                callback(result)
            if result["status"] in TERMINAL_STATES:
                return result
            time.sleep(interval)

    def _ensure_login(self) -> None:
        if not self.session_id:
            self.login()

    def _ua(self) -> str:
        return self.cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123 Safari/537.36",
        )

    def _cookies(self) -> Dict[str, str]:
        if not self.session_id:
            raise RuntimeError("Rundeck session is not initialized")
        return {"JSESSIONID": self.session_id, "communityNews": "20"}

    def _job_id_for_query(self, query: str, query_name: Optional[str] = None) -> str:
        job_ids = self.cfg.get("job_ids", {})
        if query_name and job_ids.get(query_name):
            return job_ids[query_name]

        job_id = self.cfg.get("weblog_job_id") if query.strip().lower().startswith("from weblog") else self.cfg.get("security_job_id")
        if not job_id:
            raise RuntimeError(
                "No Rundeck job id configured. Set rundeck.job_ids.<query_name> or "
                "rundeck.weblog_job_id/security_job_id in config/settings.local.yaml."
            )
        return job_id

    def _job_show_url(self, job_id: str) -> str:
        template = self.cfg.get("job_show_url_template", "{base_url}/project/{project}/job/show/{job_id}")
        return template.format(base_url=self.cfg["base_url"], project=self.cfg["project"], job_id=job_id)

    def _execution_show_url(self, execution_id: str) -> str:
        template = self.cfg.get("execution_show_url_template", "{base_url}/project/{project}/execution/show/{execution_id}")
        return template.format(base_url=self.cfg["base_url"], project=self.cfg["project"], execution_id=execution_id)

    def _job_page_context(self, job_id: str) -> Dict[str, Any]:
        url = self._job_show_url(job_id)
        resp = self.session.get(
            url,
            headers={"Referer": f"{self.cfg['base_url']}/project/{self.cfg['project']}/jobs", "User-Agent": self._ua()},
            cookies=self._cookies(),
        )
        self._raise_for_status(resp, "open Rundeck job page", url, job_id)
        soup = BeautifulSoup(resp.text, "html.parser")
        token = soup.find("input", {"name": "SYNCHRONIZER_TOKEN"})
        if not token or not token.get("value"):
            title = soup.find("title")
            title_text = title.get_text(strip=True) if title else "no page title"
            raise RuntimeError(
                "Could not locate Rundeck SYNCHRONIZER_TOKEN. "
                f"URL={url} title={title_text!r}. Verify rundeck.base_url/project/job id and user access."
            )
        form = token.find_parent("form") or soup.find("form")
        if form is None:
            raise RuntimeError(f"Could not locate Rundeck run form on {url}")
        return {"token": token["value"], "form": form, "soup": soup, "url": url}

    def _build_run_form_data(
        self,
        job_id: str,
        query: str,
        start_time: str,
        end_time: str,
        output_format: str,
        page: Dict[str, Any],
    ) -> List[Tuple[str, str]]:
        form = page["form"]
        soup = page["soup"]
        controls = self._extract_option_controls(form, soup)
        overrides = self.cfg.get("option_names", {}) if isinstance(self.cfg.get("option_names"), dict) else {}

        start_name = overrides.get("start_time") or self._find_option_name(controls, ["query start time", "start time", "starttime"], "extra.option.StartTime")
        end_name = overrides.get("end_time") or self._find_option_name(controls, ["query end time", "end time", "endtime"], "extra.option.EndTime")
        query_name = overrides.get("query") or self._find_option_name(controls, ["zclient query to execute", "query to execute", "zclient query", "query text", "query"], "extra.option.Query")
        output_name = overrides.get("output") or self._find_option_name(controls, ["output", "format"], "extra.option.Output")
        upload_name = overrides.get("upload") or self._find_option_name(controls, ["upload"], "extra.option.Upload")
        query_identifier_name = overrides.get("query_identifier") or self._find_option_name(controls, ["query identifier", "queryidentifier"], "extra.option.QueryIdentifier")
        timezone_name = overrides.get("timezone") or self._find_option_name(controls, ["timezone", "time zone"], "extra.option.timezone")
        query_type_name = overrides.get("query_type") or self._find_option_name(controls, ["query type", "querytype"], "extra.option.QueryType")
        show_hidden_name = overrides.get("show_hidden_transaction") or self._find_option_name(controls, ["show hidden transaction", "showhiddentransaction", "hidden transaction"], "extra.option.ShowHiddenTransaction")
        keystore_name = overrides.get("keystore") or self._find_option_name(controls, ["keystore"], "extra.option.Keystore")
        include_cluster_name = overrides.get("include_nanolog_cluster") or self._find_option_name(controls, ["include nanolog cluster", "includenanologcluster"], "extra.option.IncludeNanologCluster")
        exclude_cluster_name = overrides.get("exclude_nanolog_cluster") or self._find_option_name(controls, ["exclude nanolog cluster", "excludenanologcluster"], "extra.option.ExcludeNanologCluster")
        cloud_name = overrides.get("cloud") or self._find_cloud_option_name(controls, self.cfg.get("clouds", []), "extra.option.Cloud")

        form_data = self._collect_form_entries(form)
        cloud_tokens = {_norm(cloud) for cloud in self.cfg.get("clouds", [])}
        form_data = [(k, v) for k, v in form_data if _norm(v) not in cloud_tokens]

        form_data = self._set_form_values(form_data, "SYNCHRONIZER_TOKEN", page["token"])
        form_data = self._set_form_values(form_data, "SYNCHRONIZER_URI", f"/project/{self.cfg['project']}/job/show/{job_id}")
        form_data = self._set_form_values(form_data, "id", job_id)
        form_data = self._set_form_values(form_data, start_name, start_time)
        form_data = self._set_form_values(form_data, end_name, end_time)
        form_data = self._set_form_values(form_data, query_name, query)
        form_data = self._set_form_values(form_data, cloud_name, self._resolve_cloud_values(controls, cloud_name))
        form_data = self._set_form_values(form_data, include_cluster_name, " ")
        form_data = self._set_form_values(form_data, exclude_cluster_name, "")
        form_data = self._set_form_values(form_data, output_name, str(output_format).upper())
        form_data = self._set_form_values(form_data, upload_name, self.cfg.get("upload", "GoogleDrive"))
        form_data = self._set_form_values(form_data, query_identifier_name, "")

        if query.strip().lower().startswith("from weblog"):
            form_data = self._set_form_values(form_data, timezone_name, self.cfg.get("timezone", "GMT"))
            form_data = self._set_form_values(form_data, query_type_name, self.cfg.get("query_type", "web_query"))
            form_data = self._set_form_values(form_data, show_hidden_name, self.cfg.get("show_hidden_transaction", "no"))
            form_data = self._set_form_values(form_data, keystore_name, None)
        else:
            form_data = self._set_form_values(form_data, keystore_name, self.cfg.get("keystore"))
            form_data = self._set_form_values(form_data, timezone_name, None)
            form_data = self._set_form_values(form_data, query_type_name, None)
            form_data = self._set_form_values(form_data, show_hidden_name, None)

        for name, value in [
            ("extra.loglevel", "NORMAL"),
            ("followdetail", "output"),
            ("follow", "true"),
            ("_action_runJobNow", ""),
            ("extra.argString", ""),
            ("extra.nodeoverride", "filter"),
            ("max", "10"),
            ("offset", "0"),
            ("formInput", "true"),
            ("extra.nodefilter", ""),
        ]:
            form_data = self._set_form_values(form_data, name, value)

        return form_data

    def _get_user_running_jobs(self) -> List[Dict[str, Any]]:
        username = str(self.cfg.get("username", ""))
        job_ids = set(self.cfg.get("job_ids", {}).values())
        for key in ("weblog_job_id", "security_job_id"):
            if self.cfg.get(key):
                job_ids.add(self.cfg[key])

        running: List[Dict[str, Any]] = []
        for job_id in job_ids:
            url = f"{self.cfg['base_url']}/api/26/job/{job_id}/executions"
            resp = self.session.get(
                url,
                headers={"X-Rundeck-Ajax": "true", "Accept": "application/json", "User-Agent": self._ua()},
                cookies=self._cookies(),
                params={"status": "running", "max": "20"},
            )
            if resp.status_code == 403:
                continue
            self._raise_for_status(resp, "fetch running Rundeck jobs", url, job_id)
            for execution in resp.json().get("executions", []):
                exec_user = str(execution.get("user", ""))
                if username and exec_user.lower() != username.lower():
                    continue
                status = str(execution.get("status", "")).lower()
                if status not in RUNNING_STATES:
                    continue
                running.append(
                    {
                        "execution_id": str(execution.get("id", "")),
                        "job_type": "weblog" if job_id == self.cfg.get("weblog_job_id") else "security",
                        "job_name": execution.get("job", {}).get("name", ""),
                        "query": self._extract_query_from_options(execution.get("job", {}).get("options", {})),
                        "status": status,
                        "user": exec_user,
                    }
                )
        return running

    def _get_execution_output(self, execution_id: str, offset: int = 0, maxlines: int = 500) -> Dict[str, Any]:
        url = f"{self.cfg['base_url']}/api/26/execution/{execution_id}/output"
        resp = self.session.get(
            url,
            headers={
                "X-Rundeck-Ajax": "true",
                "Accept": "application/json",
                "Referer": self._execution_show_url(execution_id),
                "User-Agent": self._ua(),
            },
            cookies=self._cookies(),
            params={"offset": str(offset), "maxlines": str(maxlines)},
        )
        self._raise_for_status(resp, "fetch Rundeck execution output", url)
        return resp.json()

    def _extract_drive_link_from_logs(self, execution_id: str) -> Optional[str]:
        offset = 0
        for _ in range(int(self.cfg.get("log_pagination_limit", 100))):
            output = self._get_execution_output(execution_id, offset=offset, maxlines=500)
            for entry in output.get("entries", []):
                match = DRIVE_LINK_RE.search(str(entry.get("log", "")))
                if match:
                    return match.group(0)
            if output.get("completed", False):
                break
            new_offset = int(output.get("offset", offset) or offset)
            if new_offset == offset:
                break
            offset = new_offset
        return None

    @staticmethod
    def _extract_option_controls(form: Any, soup: BeautifulSoup) -> List[Dict[str, str]]:
        controls: List[Dict[str, str]] = []
        for control in form.find_all(["input", "textarea", "select"]):
            name = control.get("name")
            if not name or not name.startswith("extra.option."):
                continue
            if control.name == "select":
                for opt in control.find_all("option"):
                    controls.append({"name": name, "type": "select", "label": RundeckClient._control_label(control, soup), "value": opt.get("value", "")})
            else:
                control_type = (control.get("type") or control.name or "text").lower()
                value = control.get_text("", strip=True) if control.name == "textarea" else control.get("value", "")
                controls.append({"name": name, "type": control_type, "label": RundeckClient._control_label(control, soup), "value": value})
        return controls

    @staticmethod
    def _control_label(control: Any, soup: BeautifulSoup) -> str:
        control_id = control.get("id")
        if control_id:
            label = soup.find("label", {"for": control_id})
            if label:
                return label.get_text(" ", strip=True)
        sibling_text = []
        for sibling in control.previous_siblings:
            text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
            if text:
                sibling_text.append(text)
        if sibling_text:
            return " ".join(reversed(sibling_text[-2:]))
        for ancestor in control.parents:
            if getattr(ancestor, "name", None) in {"div", "fieldset", "li", "td"}:
                text = ancestor.get_text(" ", strip=True)
                if text and len(text) <= 160:
                    return text
        return ""

    @staticmethod
    def _find_option_name(controls: List[Dict[str, str]], aliases: List[str], fallback: Optional[str] = None) -> Optional[str]:
        normalized_aliases = [_norm(alias) for alias in aliases]
        for control in controls:
            haystack = " ".join([_norm(control.get("name", "")), _norm(control.get("label", "")), _norm(control.get("value", ""))])
            if any(alias and alias in haystack for alias in normalized_aliases):
                return control["name"]
        return fallback

    @staticmethod
    def _find_cloud_option_name(controls: List[Dict[str, str]], configured_clouds: List[str], fallback: str) -> str:
        cloud_tokens = {_norm(cloud) for cloud in configured_clouds}
        candidates: Dict[str, int] = {}
        for control in controls:
            if _norm(control.get("label", "")) in cloud_tokens or _norm(control.get("value", "")) in cloud_tokens:
                candidates[control["name"]] = candidates.get(control["name"], 0) + 1
        return max(candidates.items(), key=lambda item: item[1])[0] if candidates else fallback

    @staticmethod
    def _collect_form_entries(form: Any) -> List[Tuple[str, str]]:
        entries: List[Tuple[str, str]] = []
        for control in form.find_all(["input", "textarea", "select"]):
            name = control.get("name")
            if not name:
                continue
            if control.name == "input":
                input_type = (control.get("type") or "text").lower()
                if input_type in {"submit", "button", "image", "file"}:
                    continue
                if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                    continue
                entries.append((name, control.get("value", "on" if input_type in {"checkbox", "radio"} else "")))
            elif control.name == "textarea":
                entries.append((name, control.get_text("", strip=False)))
            else:
                options = control.find_all("option")
                selected = [opt.get("value", "") for opt in options if opt.has_attr("selected")]
                if not selected and options:
                    selected = [options[0].get("value", "")]
                if control.has_attr("multiple"):
                    entries.extend((name, value) for value in selected)
                elif selected:
                    entries.append((name, selected[0]))
        return entries

    @staticmethod
    def _set_form_values(entries: List[Tuple[str, str]], name: Optional[str], values: Any) -> List[Tuple[str, str]]:
        if not name:
            return entries
        filtered = [(key, value) for key, value in entries if key != name]
        if values is None:
            return filtered
        if isinstance(values, list):
            filtered.extend((name, str(value)) for value in values)
        else:
            filtered.append((name, str(values)))
        return filtered

    def _resolve_cloud_values(self, controls: List[Dict[str, str]], cloud_name: Optional[str]) -> List[str]:
        requested = self.cfg.get("clouds", [])
        if not requested or not cloud_name:
            return requested
        cloud_controls = [control for control in controls if control["name"] == cloud_name]
        resolved: List[str] = []
        for requested_cloud in requested:
            requested_token = _norm(requested_cloud)
            matched = None
            for control in cloud_controls:
                if _norm(control.get("value", "")) == requested_token or _norm(control.get("label", "")) == requested_token:
                    matched = control.get("value") or requested_cloud
                    break
            resolved.append(matched or requested_cloud)
        return resolved

    @staticmethod
    def _extract_query_from_options(options: Any) -> str:
        if isinstance(options, dict):
            for key, value in options.items():
                if isinstance(value, str) and (value.strip().lower().startswith("from ") or "query" in _norm(key)):
                    return value.strip()
        if isinstance(options, str):
            match = re.search(r"(from\s+(?:security|weblog)\s+select\s+.+)", options, re.IGNORECASE)
            return match.group(1).strip() if match else options.strip()
        return ""

    @staticmethod
    def _normalize_state(state: Any) -> str:
        lowered = str(state or "running").lower()
        return "timedout" if lowered in {"timed_out", "timeout"} else lowered

    @staticmethod
    def _extract_execution_id_from_submit_response(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        script_tag = soup.find("script", id="web_ui_token")
        if script_tag:
            try:
                payload = json.loads(script_tag.get_text(strip=True))
                match = re.search(r"/execution/show/(\d+)", payload.get("URI", ""))
                if match:
                    return match.group(1)
            except Exception:
                pass
        match = re.search(r"/execution/show/(\d+)", html)
        return match.group(1) if match else None

    @staticmethod
    def _extract_drive_file_id(link: Optional[str]) -> Optional[str]:
        if not link:
            return None
        for pattern in [r"/file/d/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"]:
            match = re.search(pattern, link)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_drive_file_name_from_link(link: Optional[str]) -> Optional[str]:
        if not link:
            return None
        match = re.search(r"([A-Za-z0-9_.-]+\.(?:current|csv))", link)
        return match.group(1) if match else None

    @staticmethod
    def _raise_for_status(resp: requests.Response, action: str, url: str, job_id: Optional[str] = None) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            snippet = re.sub(r"\s+", " ", resp.text[:500]).strip()
            job_hint = f" job_id={job_id}" if job_id else ""
            raise RuntimeError(
                f"Failed to {action}: HTTP {resp.status_code} {resp.reason}. URL={url}{job_hint}. "
                f"Response snippet={snippet!r}. If this is 404, verify rundeck.base_url, rundeck.project, "
                "and the configured job id in config/settings.local.yaml."
            ) from exc


def day_window(day: str, window_days: float = 1.0) -> Tuple[str, str]:
    day = day.strip()
    if len(day) > 10:
        start = datetime.strptime(day, "%Y-%m-%d %H:%M")
    else:
        start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=window_days) - timedelta(seconds=1)
    return start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)