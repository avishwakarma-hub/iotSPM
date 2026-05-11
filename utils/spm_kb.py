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
    "build",
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
    signature_id: str
    dep: str
    flags: str
    scope: str
    title: str
    pattern: str
    pattern_lower: str
    row_pattern: str = ""
    dependency_refids: Tuple[str, ...] = ()
    dependency_patterns: Tuple[str, ...] = ()
    match_terms: Tuple[str, ...] = ()
    support_terms: Tuple[str, ...] = ()
    family: Optional[str] = None
    family_size: int = 0
    family_members: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RawSpmRow:
    refid: str
    signature_id: str
    dep: str
    flags: str
    scope: str
    title: str
    pattern: str


class SpmKnowledgeBase:
    """Local searchable representation of released/reviewed IoT SPM UA patterns."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.signatures: List[Dict[str, Any]] = list(data.get("signatures") or [])
        # Prefer more-specific signatures first. A generic child pattern like
        # "build/" must never beat a meaningful dependency token like "tc53".
        self.signatures.sort(key=self._sort_key, reverse=True)

    @classmethod
    def load(cls, path: str | Path) -> "SpmKnowledgeBase":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def match(self, user_agent: str) -> Optional[Dict[str, Any]]:
        ua_lower = (user_agent or "").lower()
        ua_normalized = _normalize_for_match(user_agent)
        if not ua_lower or not ua_normalized.strip():
            return None
        candidates: List[Tuple[Tuple[int, int, int, int, int], Dict[str, Any]]] = []
        for sig in self.signatures:
            candidate = self._match_signature(sig, ua_lower, ua_normalized)
            if candidate:
                candidates.append(candidate)
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        return None

    @staticmethod
    def _sort_key(item: Dict[str, Any]) -> Tuple[int, int, int, int]:
        terms = [str(term) for term in (item.get("match_terms") or []) if str(term).strip()]
        return (
            len(terms),
            sum(len(term) for term in terms),
            len(str(item.get("dependency_patterns") or [])),
            len(str(item.get("pattern") or "")),
        )

    def _match_signature(
        self,
        sig: Dict[str, Any],
        ua_lower: str,
        ua_normalized: str,
    ) -> Optional[Tuple[Tuple[int, int, int, int, int], Dict[str, Any]]]:
        terms = [str(term).strip().lower() for term in (sig.get("match_terms") or []) if str(term).strip()]
        if not terms:
            # Backward compatibility for an older KB cache. Generic single-token
            # patterns are intentionally ignored to avoid false matches like
            # IoT.Device.MediaPlayer/build for every Android Build UA.
            fallback = _normalize_pattern_for_match(str(sig.get("pattern") or ""))
            if not fallback or _is_generic_match_term(fallback):
                return None
            terms = [fallback]

        matched_terms = [term for term in terms if _term_in_normalized_ua(term, ua_normalized)]
        if len(matched_terms) != len(terms):
            return None

        support_terms = [
            str(term).strip().lower()
            for term in (sig.get("support_terms") or [])
            if str(term).strip()
        ]
        matched_support_terms = [term for term in support_terms if _term_in_normalized_ua(term, ua_normalized)]
        exact_patterns = [str(sig.get("row_pattern") or ""), str(sig.get("pattern") or "")]
        exact_match = any(pattern and pattern.lower() in ua_lower for pattern in exact_patterns)
        match_type = "exact-pattern-substring" if exact_match and not sig.get("dependency_patterns") else "normalized-composite"
        score = (
            len(matched_terms),
            sum(len(term) for term in matched_terms),
            len(matched_support_terms),
            1 if exact_match else 0,
            len(str(sig.get("pattern") or "")),
        )
        return score, {
            "matched": True,
            "refid": sig.get("refid", ""),
            "smstat_id": sig.get("smstat_id") or sig.get("refid", ""),
            "signature_id": sig.get("signature_id", ""),
            "title": sig.get("title", ""),
            "pattern": sig.get("pattern", ""),
            "row_pattern": sig.get("row_pattern", ""),
            "dependency_refids": sig.get("dependency_refids") or [],
            "dependency_patterns": sig.get("dependency_patterns") or [],
            "match_terms": matched_terms,
            "support_terms": matched_support_terms,
            "family": sig.get("family") or "",
            "family_size": int(sig.get("family_size") or 0),
            "family_members": sig.get("family_members") or [],
            "match_type": match_type,
            "export_id": self.data.get("export_id", ""),
        }


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
                "smstat_id": sig.refid,
                "signature_id": sig.signature_id,
                "dep": sig.dep,
                "flags": sig.flags,
                "scope": sig.scope,
                "title": sig.title,
                "pattern": sig.pattern,
                "pattern_lower": sig.pattern_lower,
                "row_pattern": sig.row_pattern,
                "dependency_refids": list(sig.dependency_refids),
                "dependency_patterns": list(sig.dependency_patterns),
                "match_terms": list(sig.match_terms),
                "support_terms": list(sig.support_terms),
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
        "schema_version": 2,
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
    rows: Dict[str, RawSpmRow] = {}
    row_order: List[str] = []
    current_signature_id = ""
    with Path(spm_file).open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if stripped.startswith("## signature ="):
                current_signature_id = stripped.split("=", 1)[1].strip()
                continue
            if not stripped or stripped.startswith("#"):
                continue
            parts = next(csv.reader([raw_line]))
            if not parts or len(parts) < 14:
                continue
            refid = str(parts[0]).strip()
            if not refid:
                continue
            row = RawSpmRow(
                refid=refid,
                signature_id=current_signature_id,
                dep=str(parts[2]).strip() if len(parts) > 2 else "",
                flags=str(parts[3]).strip() if len(parts) > 3 else "",
                scope=str(parts[10]).strip() if len(parts) > 10 else "",
                title=str(parts[12]).strip() if len(parts) > 12 else "",
                pattern=str(parts[13]).strip() if len(parts) > 13 else "",
            )
            rows[row.refid] = row
            row_order.append(row.refid)

    parsed: List[SpmSignature] = []
    for refid in row_order:
        row = rows[refid]
        if row.scope != "2" or not _is_iot_title(row.title):
            continue
        dependency_chain = _dependency_chain(row, rows)
        row_term = _normalize_pattern_for_match(row.pattern)
        dependency_terms = [_normalize_pattern_for_match(dep.pattern) for dep in dependency_chain]
        dependency_terms = [term for term in dependency_terms if term]
        meaningful_dependency_terms = [term for term in dependency_terms if not _is_generic_match_term(term)]
        if meaningful_dependency_terms:
            match_terms = tuple(dict.fromkeys(meaningful_dependency_terms))
            support_terms = tuple(dict.fromkeys(term for term in dependency_terms if _is_generic_match_term(term)))
            pattern = _format_composite_pattern([dep.pattern for dep in dependency_chain] + [row.pattern])
        elif row_term and not _is_generic_match_term(row_term):
            match_terms = (row_term,)
            support_terms = ()
            pattern = row.pattern
        else:
            # Do not keep standalone generic final rows like MediaPlayer/build.
            # They cause broad false positives and are only useful when paired
            # with a meaningful dependency token such as tc53.
            continue
        dependency_refids = tuple(dep.refid for dep in dependency_chain if dep.refid != row.refid)
        dependency_patterns = tuple(dep.pattern for dep in dependency_chain if dep.pattern)
        parsed.append(
            SpmSignature(
                refid=row.refid,
                signature_id=row.signature_id,
                dep=row.dep,
                flags=row.flags,
                scope=row.scope,
                title=row.title,
                pattern=pattern,
                pattern_lower=pattern.lower(),
                row_pattern=row.pattern,
                dependency_refids=dependency_refids,
                dependency_patterns=dependency_patterns,
                match_terms=match_terms,
                support_terms=support_terms,
            )
        )
    return parsed


def _dependency_chain(row: RawSpmRow, rows: Dict[str, RawSpmRow]) -> List[RawSpmRow]:
    chain: List[RawSpmRow] = []
    seen = {row.refid}
    dep_id = row.dep
    while dep_id and dep_id not in seen:
        dep_row = rows.get(dep_id)
        if not dep_row:
            break
        chain.insert(0, dep_row)
        seen.add(dep_id)
        dep_id = dep_row.dep
    return chain


def _format_composite_pattern(patterns: Iterable[str]) -> str:
    clean = [re.sub(r"\s+", " ", str(pattern).strip()) for pattern in patterns if str(pattern).strip()]
    return " AND ".join(dict.fromkeys(clean))


def _normalize_for_match(value: str) -> str:
    normalized = (value or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    return f" {collapsed} " if collapsed else ""


def _normalize_pattern_for_match(pattern: str) -> str:
    value = (pattern or "").lower().strip()
    value = re.sub(r"(?i)\s*build/\s*$", " build", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _term_in_normalized_ua(term: str, ua_normalized: str) -> bool:
    normalized_term = _normalize_pattern_for_match(term)
    return bool(normalized_term) and f" {normalized_term} " in ua_normalized


def _is_generic_match_term(term: str) -> bool:
    value = _normalize_pattern_for_match(term)
    if not value:
        return True
    generic_values = {_normalize_pattern_for_match(item) for item in GENERIC_PATTERNS}
    if value in generic_values:
        return True
    return len(value) < 4


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
