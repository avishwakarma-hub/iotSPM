from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


GENERIC_PATTERNS = {
    "android",
    "applewebkit",
    "build/",
    "chrome",
    "cfnetwork/",
    "dalvik",
    "firefox",
    "linux",
    "like mac os",
    "mobile",
    "mozilla",
    "okhttp",
    "safari",
    "version/",
    "webkit",
}


@dataclass(frozen=True)
class SpmSignature:
    refid: str
    dep: str
    flags: str
    scope: str
    title: str
    pattern: str
    pattern_lower: str
    family: Optional[str] = None
    family_size: int = 0
    family_members: Tuple[str, ...] = ()


class SpmKnowledgeBase:
    """Local searchable representation of released/reviewed IoT SPM UA patterns."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.signatures: List[Dict[str, Any]] = list(data.get("signatures") or [])
        # Prefer more-specific patterns first. This avoids a short family pattern
        # taking precedence over a longer exact SPM pattern.
        self.signatures.sort(key=lambda item: len(str(item.get("pattern") or "")), reverse=True)

    @classmethod
    def load(cls, path: str | Path) -> "SpmKnowledgeBase":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def match(self, user_agent: str) -> Optional[Dict[str, Any]]:
        ua_lower = (user_agent or "").lower()
        if not ua_lower:
            return None
        for sig in self.signatures:
            pattern = str(sig.get("pattern") or "")
            if pattern and pattern.lower() in ua_lower:
                return {
                    "matched": True,
                    "refid": sig.get("refid", ""),
                    "title": sig.get("title", ""),
                    "pattern": pattern,
                    "family": sig.get("family") or "",
                    "family_size": int(sig.get("family_size") or 0),
                    "family_members": sig.get("family_members") or [],
                    "match_type": "pattern-substring",
                    "export_id": self.data.get("export_id", ""),
                }
        return None


def load_knowledge_base(path: str | Path | None) -> Optional[SpmKnowledgeBase]:
    if not path:
        return None
    kb_path = Path(path)
    if not kb_path.is_file():
        return None
    return SpmKnowledgeBase.load(kb_path)


def save_knowledge_base(kb_data: Dict[str, Any], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(kb_data, handle, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(temp_path, output_path)
    return output_path


def build_knowledge_base(
    spm_file: str | Path,
    *,
    export_id: str | int | None = None,
    source_url: str = "",
) -> Dict[str, Any]:
    signatures = parse_spm_file(spm_file)
    by_title: Dict[str, List[SpmSignature]] = defaultdict(list)
    for sig in signatures:
        by_title[sig.title].append(sig)

    family_members: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    families_by_sig: Dict[Tuple[str, str, str], Optional[str]] = {}
    for title, title_sigs in by_title.items():
        patterns = [sig.pattern for sig in title_sigs]
        for sig in title_sigs:
            family = extract_family_key(sig.pattern, patterns)
            families_by_sig[(sig.refid, sig.title, sig.pattern)] = family
            if family:
                family_members[(title, family)].append(sig.pattern)

    serialized_sigs: List[Dict[str, Any]] = []
    families: Dict[str, Dict[str, Any]] = {}
    for sig in signatures:
        family = families_by_sig[(sig.refid, sig.title, sig.pattern)]
        members = family_members.get((sig.title, family), []) if family else []
        family_size = len(set(members)) if family else 0
        serialized_sigs.append(
            {
                "refid": sig.refid,
                "dep": sig.dep,
                "flags": sig.flags,
                "scope": sig.scope,
                "title": sig.title,
                "pattern": sig.pattern,
                "pattern_lower": sig.pattern_lower,
                "family": family or "",
                "family_size": family_size,
                "family_members": sorted(set(members)),
            }
        )
        if family:
            key = f"{sig.title}|{family}"
            families[key] = {
                "device_type": sig.title,
                "family": family,
                "size": family_size,
                "patterns": sorted(set(members)),
            }

    return {
        "schema_version": 1,
        "export_id": export_id or "local",
        "source_url": source_url,
        "spm_file": str(spm_file),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_iot_ua_sigs": len(signatures),
        "total_meaningful": len(signatures),
        "signatures": serialized_sigs,
        "families": dict(sorted(families.items(), key=lambda item: (-int(item[1].get("size") or 0), item[0]))),
    }


def parse_spm_file(spm_file: str | Path) -> List[SpmSignature]:
    parsed: List[SpmSignature] = []
    with Path(spm_file).open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for parts in reader:
            if not parts:
                continue
            first = str(parts[0]).strip()
            if first.startswith("#") or len(parts) < 14:
                continue
            refid = first
            dep = str(parts[2]).strip() if len(parts) > 2 else ""
            flags = str(parts[3]).strip() if len(parts) > 3 else ""
            scope = str(parts[10]).strip() if len(parts) > 10 else ""
            title = str(parts[12]).strip() if len(parts) > 12 else ""
            pattern = str(parts[13]).strip() if len(parts) > 13 else ""
            if scope != "2" or not _is_iot_title(title) or _is_generic_pattern(pattern):
                continue
            parsed.append(
                SpmSignature(
                    refid=refid,
                    dep=dep,
                    flags=flags,
                    scope=scope,
                    title=title,
                    pattern=pattern,
                    pattern_lower=pattern.lower(),
                )
            )
    return parsed


def extract_family_key(pattern: str, all_patterns_for_type: Iterable[str]) -> Optional[str]:
    clean = _clean_pattern_for_family(pattern)
    if len(clean) < 3:
        return None
    cleaned_patterns = [_clean_pattern_for_family(item) for item in all_patterns_for_type]

    token = clean.lower()
    candidates = _family_candidates(token)
    for candidate in candidates:
        if _has_family_sibling(candidate, token, cleaned_patterns):
            return candidate.rstrip("-_")

    best_lcp = ""
    for other in cleaned_patterns:
        other_lower = other.lower()
        if other_lower == token:
            continue
        lcp = _longest_common_prefix(token, other_lower).strip("-_; /()")
        if len(lcp) >= 4 and len(lcp) > len(best_lcp):
            best_lcp = lcp
    return best_lcp or None


def _family_candidates(token: str) -> List[str]:
    candidates: List[str] = []
    patterns = [
        r"^([a-z]+\d+[-_]?)",  # mc9-*, g16*, tc52ax-style series
        r"^([a-z]+[-_])",  # vr-*, ar-*, sip-*
        r"^([a-z]{3,})",  # dezl*, kindle-like if siblings exist
        r"^([a-z]+\s+[a-z]+)",  # braille note style model family
    ]
    for regex in patterns:
        match = re.match(regex, token)
        if not match:
            continue
        candidate = match.group(1).strip()
        if len(candidate.rstrip("-_")) >= 2 and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _has_family_sibling(candidate: str, token: str, cleaned_patterns: Iterable[str]) -> bool:
    candidate_lower = candidate.lower()
    for other in cleaned_patterns:
        other_lower = other.lower()
        if other_lower != token and other_lower.startswith(candidate_lower):
            return True
    return False


def _clean_pattern_for_family(pattern: str) -> str:
    value = (pattern or "").strip()
    value = re.sub(r"(?i)\s*build/.*$", "", value)
    value = re.sub(r"[);]+$", "", value)
    value = value.strip(" ;/()")
    return value


def _longest_common_prefix(left: str, right: str) -> str:
    idx = 0
    limit = min(len(left), len(right))
    while idx < limit and left[idx] == right[idx]:
        idx += 1
    return left[:idx]


def _is_iot_title(title: str) -> bool:
    return title.startswith("IoT.Device") or title.startswith("IoT.")


def _is_generic_pattern(pattern: str) -> bool:
    value = (pattern or "").strip().lower()
    return not value or len(value) < 4 or value in GENERIC_PATTERNS
