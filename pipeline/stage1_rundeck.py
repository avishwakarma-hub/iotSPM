from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import requests
import urllib3
from bs4 import BeautifulSoup

TIME_FORMAT = "%d%b%Y:%H:%M:%S"


class RundeckClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg["rundeck"]
        self.session = requests.Session()
        self.session.verify = bool(self.cfg.get("verify_ssl", False))
        if not self.session.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session_id: Optional[str] = None

    def login(self) -> None:
        url = f"{self.cfg['base_url']}/j_security_check"
        resp = self.session.post(
            url,
            headers={
                "Origin": self.cfg["base_url"],
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{self.cfg['base_url']}/user/login",
                "User-Agent": self.cfg.get("user_agent"),
            },
            data={"j_username": self.cfg["username"], "j_password": self.cfg["password"]},
        )
        resp.raise_for_status()
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
        token = self._csrf_token(job_id)
        run_url = self._job_show_url(job_id)
        form_data = [
            ("SYNCHRONIZER_TOKEN", token),
            ("extra.option.Query", query),
            ("extra.option.StartTime", start_time),
            ("extra.option.EndTime", end_time),
            ("extra.option.OutputFormat", output_format or self.cfg.get("output_format", "CSV")),
        ]
        for cloud in self.cfg.get("clouds", []):
            form_data.append(("extra.option.Cloud", cloud))
        form_data.append(("_action_launchNow", "Run Now"))

        resp = self.session.post(
            run_url,
            headers={
                "Referer": run_url,
                "Origin": self.cfg["base_url"],
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.cfg.get("user_agent"),
            },
            cookies={"JSESSIONID": self.session_id, "communityNews": "20"},
            data=form_data,
        )
        self._raise_for_status(resp, "submit Rundeck query", run_url, job_id)
        execution_id = self._extract_execution_id(resp.text)
        if not execution_id:
            raise RuntimeError("Query submitted but execution id could not be extracted")
        return execution_id

    def poll_execution(self, execution_id: str) -> Dict[str, Any]:
        self._ensure_login()
        url = self._execution_show_url(execution_id)
        resp = self.session.get(url, headers={"User-Agent": self.cfg.get("user_agent")})
        self._raise_for_status(resp, "poll Rundeck execution", url)
        html = resp.text
        status = self._extract_status(html)
        drive_file_id, drive_file_name = self._extract_drive_file(html)
        return {
            "execution_id": execution_id,
            "status": status,
            "drive_file_id": drive_file_id,
            "drive_file_name": drive_file_name,
            "url": url,
        }

    def wait_for_completion(self, execution_id: str, callback=None) -> Dict[str, Any]:
        interval = int(self.cfg.get("poll_interval_seconds", 300))
        while True:
            result = self.poll_execution(execution_id)
            if callback:
                callback(result)
            if result["status"] in {"succeeded", "failed", "aborted", "timedout"}:
                return result
            time.sleep(interval)

    def _ensure_login(self) -> None:
        if not self.session_id:
            self.login()

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
        return template.format(base_url=self.cfg["base_url"].rstrip("/"), project=self.cfg["project"], job_id=job_id)

    def _execution_show_url(self, execution_id: str) -> str:
        template = self.cfg.get("execution_show_url_template", "{base_url}/project/{project}/execution/show/{execution_id}")
        return template.format(base_url=self.cfg["base_url"].rstrip("/"), project=self.cfg["project"], execution_id=execution_id)

    def _csrf_token(self, job_id: str) -> str:
        url = self._job_show_url(job_id)
        resp = self.session.get(url, headers={"User-Agent": self.cfg.get("user_agent")})
        self._raise_for_status(resp, "open Rundeck job page", url, job_id)
        soup = BeautifulSoup(resp.text, "html.parser")
        token = soup.find("input", {"name": "SYNCHRONIZER_TOKEN"})
        if not token or not token.get("value"):
            title = soup.find("title")
            title_text = title.get_text(strip=True) if title else "no page title"
            raise RuntimeError(
                "Could not locate Rundeck SYNCHRONIZER_TOKEN. "
                f"URL={url} title={title_text!r}. This usually means the configured job id/path is wrong "
                "or the logged-in user cannot access that job. Open the URL in a browser and copy the working job URL/job UUID."
            )
        return token["value"]

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

    @staticmethod
    def _extract_execution_id(html: str) -> Optional[str]:
        match = re.search(r"/execution/show/(\d+)", html)
        return match.group(1) if match else None

    @staticmethod
    def _extract_status(html: str) -> str:
        lowered = html.lower()
        for status in ["succeeded", "failed", "aborted", "timedout", "running", "scheduled"]:
            if status in lowered:
                return status
        return "running"

    @staticmethod
    def _extract_drive_file(html: str) -> Tuple[Optional[str], Optional[str]]:
        patterns = [
            r"https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)",
            r"https://drive\.google\.com/open\?id=([A-Za-z0-9_-]+)",
            r"fileId=([A-Za-z0-9_-]+)",
        ]
        file_id = None
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                file_id = match.group(1)
                break
        name_match = re.search(r"([A-Za-z0-9_.-]+\.(?:current|csv))", html)
        return file_id, name_match.group(1) if name_match else None


def day_window(day: str) -> Tuple[str, str]:
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)
