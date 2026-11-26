"""Pearl Lemon identity resolution stage."""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, cast
from urllib.parse import urlsplit

from agencies.adl.identity.entity_resolution import EntityResolver
from agencies.pearl_lemon.normalizer import DEFAULT_NORMALIZED_RECORDS_PATH

DEFAULT_RESOLVED_RECORDS_PATH = (
    Path(__file__).parent / "identity_resolution" / "resolved_records.jsonl"
)

_STATUS_CONFIRMED = "confirmed_match"
_STATUS_POSSIBLE = "possible_match"
_STATUS_DISTINCT = "distinct_entity"
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa)\b\.?",
    flags=re.IGNORECASE,
)
_NOISY_TITLE_PREFIX_RE = re.compile(
    r"^(?:top\s+\d+|best\s+\d*|leading\s+\d*|list\s+of|directory\s+of|guide\s+to|review\s+of)\b",
    flags=re.IGNORECASE,
)
_GENERIC_NAME_TOKENS = frozenset(
    {
        "ai",
        "analytics",
        "app",
        "apps",
        "cloud",
        "consulting",
        "data",
        "digital",
        "enterprise",
        "global",
        "group",
        "hub",
        "hq",
        "intelligence",
        "international",
        "labs",
        "media",
        "network",
        "online",
        "platform",
        "platforms",
        "pro",
        "services",
        "software",
        "solutions",
        "systems",
        "tech",
        "technologies",
        "technology",
        "tools",
        "ventures",
        "web",
        "world",
        "worldwide",
    }
)
_ARTIFACT_IGNORE_RULES = (
    "identity_resolution/*.lock",
    "identity_resolution/*.tmp",
    "normalized_records/*.lock",
    "normalized_records/*.tmp",
)


def _validate_similarity_threshold(similarity_threshold: Any) -> float:
    """Validate that similarity_threshold is a finite float in [0.0, 1.0]."""
    if isinstance(similarity_threshold, bool) or not isinstance(
        similarity_threshold, (int, float)
    ):
        raise TypeError(
            "similarity_threshold must be a finite real number between 0 and 1"
        )
    threshold = float(similarity_threshold)
    if not math.isfinite(threshold) or not (0.0 <= threshold <= 1.0):
        raise ValueError("similarity_threshold must be between 0 and 1")
    return threshold


def _validate_json_serializable(value: Any, *, label: str) -> None:
    """Ensure a payload serializes cleanly to strict JSON before disk I/O."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not JSON-serializable: {exc}") from exc


def _validate_distinct_io_paths(input_path: Path, output_path: Path) -> None:
    """Reject identical input and output paths so input snapshots are never overwritten."""
    resolved_input = input_path.resolve(strict=False)
    resolved_output = output_path.resolve(strict=False)
    if resolved_input == resolved_output:
        raise ValueError(
            f"input_path and output_path must be distinct; both resolve to {resolved_output}"
        )


def _ensure_artifact_gitignore(base_dir: Path, output_path: Path | None = None) -> None:
    """Ensure *.lock and *.tmp under identity_resolution/ and normalized_records/ are ignored."""
    rules: list[str] = list(_ARTIFACT_IGNORE_RULES)
    if output_path is not None:
        lock_name = f".{output_path.name}.lock"
        tmp_pattern = f".{output_path.name}.*.tmp"
        parent_name = output_path.parent.name
        if parent_name in {"identity_resolution", "normalized_records"}:
            rules.extend(
                [
                    f"{parent_name}/{lock_name}",
                    f"{parent_name}/{tmp_pattern}",
                ]
            )
        else:
            rules.extend([lock_name, tmp_pattern])

    gitignore_path = base_dir / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        try:
            existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
    elif not (base_dir / "identity_resolution").exists() and not (
        base_dir / "normalized_records"
    ).exists():
        return

    existing_set = {line.strip() for line in existing_lines}
    missing = [rule for rule in rules if rule not in existing_set]
    if not missing:
        return

    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        content = "\n".join(existing_lines)
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n".join(missing) + "\n"
        gitignore_path.write_text(content, encoding="utf-8")
    except OSError:
        return


@contextmanager
def _exclusive_output_lock(output_path: Path) -> Iterator[None]:
    """Acquire an advisory exclusive lock for output_path publication across processes/threads."""
    target_dir = output_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / f".{output_path.name}.lock"
    lock_fd: int | None = None
    try:
        flags = os.O_CREAT | os.O_RDWR
        o_nofollow = getattr(os, "O_NOFOLLOW", 0)
        if o_nofollow:
            flags |= o_nofollow
        lock_fd = os.open(str(lock_path), flags, 0o600)

        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except ImportError:
            if os.name == "nt":
                import msvcrt

                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if lock_fd is not None:
            try:
                try:
                    import fcntl

                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except ImportError:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(lock_fd, 0, os.SEEK_SET)
                        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            finally:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of parent directory entry after atomic file replacement."""
    o_rdonly = getattr(os, "O_RDONLY", None)
    if o_rdonly is None or not hasattr(os, "fsync"):
        return
    dir_fd: int | None = None
    try:
        dir_fd = os.open(str(directory), o_rdonly)
        os.fsync(dir_fd)
    except (OSError, NotImplementedError, PermissionError):
        # Some filesystems, virtualized mounts, or OSes do not support directory fsync.
        return
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned or None


def _is_noisy_or_generic_title(name: str | None) -> bool:
    """Return True when a candidate name looks like an editorial/listicle title or pure generic label."""
    cleaned = _clean_text(name)
    if not cleaned:
        return True
    if _NOISY_TITLE_PREFIX_RE.search(cleaned):
        return True
    stripped = _LEGAL_SUFFIX_RE.sub(" ", cleaned.casefold())
    raw_tokens = [tok for tok in re.findall(r"[a-z0-9]+", stripped) if len(tok) > 1]
    if not raw_tokens:
        return True
    if all(tok in _GENERIC_NAME_TOKENS for tok in raw_tokens):
        return True
    return False


def _normalize_domain(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip().casefold()
    if not trimmed:
        return None
    candidate = trimmed if "://" in trimmed else f"//{trimmed.lstrip('/')}"
    try:
        host = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    return host.removeprefix("www.")


def _canonical_name_tokens(record: Mapping[str, Any]) -> set[str]:
    for key in (
        "canonical_name",
        "company_name_normalized",
        "verified_company_name",
        "company_name",
    ):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            cleaned = _LEGAL_SUFFIX_RE.sub(" ", val.casefold())
            all_tokens = {
                tok for tok in re.findall(r"[a-z0-9]+", cleaned) if len(tok) > 1
            }
            if not all_tokens:
                continue
            distinctive = {
                tok for tok in all_tokens if tok not in _GENERIC_NAME_TOKENS
            }
            return distinctive if distinctive else all_tokens
    return set()


def _name_similarity(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    toks_a = _canonical_name_tokens(a)
    toks_b = _canonical_name_tokens(b)
    if not toks_a or not toks_b:
        return 0.0
    if toks_a == toks_b:
        return 1.0
    overlap = len(toks_a & toks_b)
    union = len(toks_a | toks_b)
    return overlap / union if union else 0.0


def _extract_company_domains(record: Mapping[str, Any]) -> list[str]:
    """Extract domains belonging to the candidate company itself (never source URLs)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for key in ("canonical_domain", "domain", "website"):
        norm = _normalize_domain(record.get(key))
        if norm and norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    meta = record.get("extraction_metadata")
    if isinstance(meta, Mapping):
        for key in ("canonical_domain", "domain"):
            norm = _normalize_domain(meta.get(key))
            if norm and norm not in seen:
                seen.add(norm)
                ordered.append(norm)
    return ordered


def _extract_provenance_domains(record: Mapping[str, Any]) -> list[str]:
    """Extract source/provenance page domains (e.g., articles, directories, case studies)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for key in ("source_url",):
        norm = _normalize_domain(record.get(key))
        if norm and norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    meta = record.get("extraction_metadata")
    if isinstance(meta, Mapping):
        for key in ("source_url",):
            norm = _normalize_domain(meta.get(key))
            if norm and norm not in seen:
                seen.add(norm)
                ordered.append(norm)
    return ordered


def _collect_string_list(record: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw = record.get(key)
        if isinstance(raw, str):
            cleaned = _clean_text(raw)
            if cleaned and cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                items.append(cleaned)
        elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, dict)):
            for entry in raw:
                if isinstance(entry, str):
                    cleaned = _clean_text(entry)
                    if cleaned and cleaned.casefold() not in seen:
                        seen.add(cleaned.casefold())
                        items.append(cleaned)
                elif isinstance(entry, Mapping):
                    for sub_key in ("name", "title", "label", "value", "domain", "city"):
                        cleaned = _clean_text(entry.get(sub_key))
                        if cleaned and cleaned.casefold() not in seen:
                            seen.add(cleaned.casefold())
                            items.append(cleaned)
    return items


class PearlLemonDeduplicator(EntityResolver):
    """Deduplicate and resolve normalized candidates into a JSONL entity snapshot."""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        *,
        normalized_records: Iterable[dict[str, Any]] | None = None,
        input_path: str | Path = DEFAULT_NORMALIZED_RECORDS_PATH,
        output_path: str | Path = DEFAULT_RESOLVED_RECORDS_PATH,
        pipeline_context: Mapping[str, Any] | None = None,
        use_base_resolver_output: bool = False,
    ) -> None:
        validated_threshold = _validate_similarity_threshold(similarity_threshold)
        resolved_input_path = Path(input_path)
        resolved_output_path = Path(output_path)
        _validate_distinct_io_paths(resolved_input_path, resolved_output_path)

        super().__init__(similarity_threshold=validated_threshold)
        self.similarity_threshold = validated_threshold
        self.input_path = resolved_input_path
        self.output_path = resolved_output_path
        self.use_base_resolver_output = bool(use_base_resolver_output)
        self.normalized_records = (
            list(normalized_records)
            if normalized_records is not None
            else self._read_records(self.input_path)
        )
        if pipeline_context is not None:
            context_copy = copy.deepcopy(dict(pipeline_context))
            _validate_json_serializable(context_copy, label="pipeline_context")
            self.pipeline_context: dict[str, Any] | None = context_copy
        else:
            self.pipeline_context = None
        self.resolved_records: list[dict[str, Any]] = []

    @staticmethod
    def _read_records(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as input_file:
            for line_number, line in enumerate(input_file, start=1):
                if not line.strip():
                    continue
                try:
                    record_value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in normalized records file {path} "
                        f"at line {line_number}"
                    ) from exc
                if not isinstance(record_value, dict):
                    raise ValueError(
                        f"Expected a JSON object in {path} at line {line_number}"
                    )
                records.append(cast(dict[str, Any], record_value))
        return records

    @staticmethod
    def _canonical_sort_key(record: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
        has_verified_name = 0 if record.get("verified_company_name") else 1
        candidate_name = (
            record.get("verified_company_name")
            or record.get("company_name_normalized")
            or record.get("company_name")
        )
        is_noisy_name = 1 if _is_noisy_or_generic_title(candidate_name) else 0
        domains = _extract_company_domains(record)
        has_domain = 0 if domains else 1
        name_str = str(candidate_name or "").casefold()
        domain_str = domains[0] if domains else ""
        return (has_verified_name, is_noisy_name, has_domain, name_str, domain_str)

    @classmethod
    def _pair_relation(
        cls,
        a: Mapping[str, Any],
        b: Mapping[str, Any],
        similarity_threshold: float,
    ) -> tuple[str, float]:
        """Return (relation, confidence) using company domains (not source_url provenance)."""
        company_domains_a = set(_extract_company_domains(a))
        company_domains_b = set(_extract_company_domains(b))
        shared_company_domains = company_domains_a & company_domains_b
        sim = _name_similarity(a, b)

        if shared_company_domains:
            conf = min(0.99, round(0.90 + 0.09 * max(sim, 0.5), 4))
            return "confirmed", conf

        # Distinct explicit company domains are never confirmed duplicates via name
        # similarity alone; hold as 'possible' if names are similar.
        if company_domains_a and company_domains_b and not shared_company_domains:
            if sim >= similarity_threshold:
                return "possible", round(min(0.75, max(0.45, sim * 0.75)), 4)
            return "distinct", round(max(0.1, 1.0 - sim), 4)

        if sim >= similarity_threshold and (
            a.get("verified_company_name") and b.get("verified_company_name")
        ):
            return "confirmed", round(min(0.95, max(0.80, sim)), 4)
        if sim >= similarity_threshold:
            return "possible", round(min(0.74, max(0.40, sim * 0.72)), 4)
        return "distinct", round(max(0.1, 1.0 - sim), 4)

    @classmethod
    def _stable_entity_id(
        cls,
        canonical_name: str | None,
        canonical_domain: str | None,
    ) -> str:
        dom_part = (canonical_domain or "").strip().casefold()
        name_part = (canonical_name or "").strip().casefold()
        seed = f"{dom_part}|{name_part}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        return f"ent_{digest}"

    @classmethod
    def _fallback_record_fingerprint(
        cls,
        member: Mapping[str, Any],
        *,
        occurrence: int = 0,
    ) -> str:
        """Build a deterministic fallback fingerprint when an input record has no explicit ID."""
        comp_doms = ",".join(sorted(_extract_company_domains(member)))
        prov_doms = ",".join(sorted(_extract_provenance_domains(member)))
        names = "|".join(
            (
                _clean_text(member.get("verified_company_name"))
                or _clean_text(member.get("company_name_normalized"))
                or _clean_text(member.get("company_name"))
                or ""
            ).casefold()
            for _ in (0,)
        )
        raw_names = "|".join(
            (_clean_text(member.get(k)) or "").casefold()
            for k in (
                "verified_company_name",
                "company_name_normalized",
                "company_name",
                "brand_name",
                "legal_name",
            )
        )
        seed = f"{comp_doms}|{prov_doms}|{names}|{raw_names}|occ={occurrence}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        return f"src_{digest}"

    @classmethod
    def _extract_source_record_ids(
        cls, members: list[dict[str, Any]]
    ) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        occurrences: dict[str, int] = {}

        for member in members:
            explicit_id: str | None = None
            for key in ("record_id", "id", "candidate_id", "source_record_id"):
                val = member.get(key)
                if isinstance(val, (str, int)) and not isinstance(val, bool):
                    s = str(val).strip()
                    if s:
                        explicit_id = s
                        break

            if explicit_id is not None:
                if explicit_id not in seen:
                    seen.add(explicit_id)
                    ids.append(explicit_id)
            else:
                base_fp = cls._fallback_record_fingerprint(member, occurrence=0)
                occ = occurrences.get(base_fp, 0)
                occurrences[base_fp] = occ + 1
                fp = (
                    base_fp
                    if occ == 0
                    else cls._fallback_record_fingerprint(member, occurrence=occ)
                )
                if fp not in seen:
                    seen.add(fp)
                    ids.append(fp)

        return sorted(ids)

    @classmethod
    def _expand_to_input_observations(
        cls,
        group_records: list[dict[str, Any]],
        raw_input_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expand group records back to underlying normalized inputs when available."""
        if not raw_input_records:
            return group_records

        matched_inputs: list[dict[str, Any]] = []
        used_indices: set[int] = set()

        for grp_rec in group_records:
            grp_domains = set(_extract_company_domains(grp_rec))
            grp_tokens = _canonical_name_tokens(grp_rec)
            for idx, inp_rec in enumerate(raw_input_records):
                if idx in used_indices:
                    continue
                inp_domains = set(_extract_company_domains(inp_rec))
                inp_tokens = _canonical_name_tokens(inp_rec)
                domain_match = bool(
                    grp_domains and inp_domains and (grp_domains & inp_domains)
                )
                name_match = bool(grp_tokens and inp_tokens and grp_tokens == inp_tokens)
                if domain_match or name_match:
                    matched_inputs.append(copy.deepcopy(inp_rec))
                    used_indices.add(idx)

        return (
            matched_inputs
            if matched_inputs
            else [copy.deepcopy(r) for r in group_records]
        )

    @classmethod
    def _select_canonical_name(
        cls,
        observation_members: list[dict[str, Any]],
        canonical_domain: str | None,
    ) -> str | None:
        """Select a clean canonical name while avoiding noisy or generic listicle titles."""
        for member in observation_members:
            for key in (
                "verified_company_name",
                "company_name_normalized",
                "company_name",
            ):
                val = _clean_text(member.get(key))
                if val and not _is_noisy_or_generic_title(val):
                    return val

        for member in observation_members:
            for key in (
                "verified_company_name",
                "company_name_normalized",
                "company_name",
            ):
                val = _clean_text(member.get(key))
                if val:
                    return val

        return canonical_domain

    @classmethod
    def _build_entity_record(
        cls,
        members: list[dict[str, Any]],
        *,
        status: str,
        action: str,
        confidence: float,
        raw_input_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        ordered = sorted(members, key=cls._canonical_sort_key)
        canonical = copy.deepcopy(ordered[0])
        observation_members = (
            cls._expand_to_input_observations(ordered, raw_input_records)
            if raw_input_records is not None
            else ordered
        )
        observation_members = sorted(observation_members, key=cls._canonical_sort_key)

        all_company_domains: list[str] = []
        seen_company_domains: set[str] = set()
        all_provenance_domains: list[str] = []
        seen_provenance_domains: set[str] = set()

        for member in observation_members:
            for dom in _extract_company_domains(member):
                if dom not in seen_company_domains:
                    seen_company_domains.add(dom)
                    all_company_domains.append(dom)
            for prov_dom in _extract_provenance_domains(member):
                if prov_dom not in seen_provenance_domains:
                    seen_provenance_domains.add(prov_dom)
                    all_provenance_domains.append(prov_dom)

        canonical_domain = all_company_domains[0] if all_company_domains else None
        alternate_domains = (
            all_company_domains[1:] if len(all_company_domains) > 1 else []
        )
        canonical_name = cls._select_canonical_name(
            observation_members, canonical_domain
        )

        aliases: list[str] = []
        seen_aliases: set[str] = set()
        brand_names: list[str] = []
        legal_names: list[str] = []
        trading_names: list[str] = []
        parent_companies: list[str] = []
        subsidiaries: list[str] = []

        emp_obs: list[dict[str, Any]] = []
        loc_obs: list[dict[str, Any]] = []
        svc_obs: list[dict[str, Any]] = []
        tech_obs: list[dict[str, Any]] = []
        source_observations: list[dict[str, Any]] = []

        for member_idx, member in enumerate(observation_members):
            member_company_domains = _extract_company_domains(member)
            member_provenance_domains = _extract_provenance_domains(member)
            member_name = (
                _clean_text(member.get("verified_company_name"))
                or _clean_text(member.get("company_name"))
            )

            for field in (
                "company_name",
                "verified_company_name",
                "brand_name",
                "legal_name",
                "trading_name",
            ):
                val = _clean_text(member.get(field))
                if val and val.casefold() not in seen_aliases:
                    seen_aliases.add(val.casefold())
                    aliases.append(val)

            for b_name in _collect_string_list(member, ("brand_name", "brand_names")):
                if b_name.casefold() not in {x.casefold() for x in brand_names}:
                    brand_names.append(b_name)
            for l_name in _collect_string_list(
                member, ("legal_name", "legal_names", "registered_name")
            ):
                if l_name.casefold() not in {x.casefold() for x in legal_names}:
                    legal_names.append(l_name)
            for t_name in _collect_string_list(
                member, ("trading_name", "trading_names", "dba")
            ):
                if t_name.casefold() not in {x.casefold() for x in trading_names}:
                    trading_names.append(t_name)

            # Keep corporate lineage distinct from candidate self-identity so
            # parent/subsidiary metrics or ICP fit are never conflated.
            for p_co in _collect_string_list(
                member, ("parent_company", "parent_organization", "parent_name")
            ):
                if p_co.casefold() not in {x.casefold() for x in parent_companies}:
                    parent_companies.append(p_co)
            for sub_co in _collect_string_list(
                member, ("subsidiaries", "subsidiary_names", "child_companies")
            ):
                if sub_co.casefold() not in {x.casefold() for x in subsidiaries}:
                    subsidiaries.append(sub_co)

            is_parent_headcount = member.get("parent_company_headcount") is True
            if (
                member.get("employee_count") is not None
                or member.get("verified_employee_count") is not None
                or member.get("employee_count_observation") is not None
            ):
                emp_obs.append(
                    {
                        "member_index": member_idx,
                        "entity_name": member_name,
                        "domain": (
                            member_company_domains[0]
                            if member_company_domains
                            else None
                        ),
                        "value": copy.deepcopy(member.get("employee_count")),
                        "verified_value": member.get("verified_employee_count"),
                        "observation": copy.deepcopy(
                            member.get("employee_count_observation")
                        ),
                        "parent_company_headcount": is_parent_headcount,
                        "attributed_to_self": not is_parent_headcount,
                    }
                )

            for loc in _collect_string_list(
                member, ("locations", "location", "headquarters", "city", "country")
            ):
                loc_obs.append(
                    {
                        "member_index": member_idx,
                        "entity_name": member_name,
                        "domain": (
                            member_company_domains[0]
                            if member_company_domains
                            else None
                        ),
                        "value": loc,
                    }
                )

            for svc in _collect_string_list(
                member, ("services", "service_lines", "offerings", "categories")
            ):
                svc_obs.append(
                    {
                        "member_index": member_idx,
                        "entity_name": member_name,
                        "domain": (
                            member_company_domains[0]
                            if member_company_domains
                            else None
                        ),
                        "value": svc,
                    }
                )

            for tech in _collect_string_list(
                member, ("technologies", "tech_stack", "stack")
            ):
                tech_obs.append(
                    {
                        "member_index": member_idx,
                        "entity_name": member_name,
                        "domain": (
                            member_company_domains[0]
                            if member_company_domains
                            else None
                        ),
                        "value": tech,
                    }
                )

            source_observations.append(
                {
                    "member_index": member_idx,
                    "company_name": member.get("company_name"),
                    "company_name_normalized": member.get("company_name_normalized"),
                    "verified_company_name": member.get("verified_company_name"),
                    "company_domains": member_company_domains,
                    "provenance_domains": member_provenance_domains,
                    "employee_count": copy.deepcopy(member.get("employee_count")),
                    "verified_employee_count": member.get("verified_employee_count"),
                    "parent_company_headcount": is_parent_headcount,
                    "raw_observations": copy.deepcopy(member.get("raw_observations")),
                    "extraction_metadata": copy.deepcopy(
                        member.get("extraction_metadata")
                    ),
                }
            )

        entity_id = cls._stable_entity_id(canonical_name, canonical_domain)
        source_record_ids = cls._extract_source_record_ids(observation_members)

        canonical["entity_id"] = entity_id
        canonical["canonical_name"] = canonical_name
        canonical["canonical_domain"] = canonical_domain
        canonical["aliases"] = sorted(aliases, key=str.casefold)
        canonical["source_record_ids"] = source_record_ids
        canonical["identity_confidence"] = confidence
        canonical["identity_status"] = status
        canonical["resolution_status"] = status
        canonical["resolution_action"] = action
        canonical["alternate_domains"] = sorted(alternate_domains)
        canonical["entity_relationships"] = {
            "brand_names": sorted(brand_names, key=str.casefold),
            "legal_names": sorted(legal_names, key=str.casefold),
            "trading_names": sorted(trading_names, key=str.casefold),
            "parent_company": sorted(parent_companies, key=str.casefold),
            "subsidiaries": sorted(subsidiaries, key=str.casefold),
            "operating_domains": sorted(all_company_domains),
            "alternate_domains": sorted(alternate_domains),
            "provenance_domains": sorted(all_provenance_domains),
        }
        canonical["observations"] = {
            "employee_count": emp_obs,
            "locations": loc_obs,
            "services": svc_obs,
            "technologies": tech_obs,
        }
        canonical["source_observations"] = source_observations
        canonical["corroborated_member_count"] = len(observation_members)
        return canonical

    @classmethod
    def _enforce_non_transitive_resolution(
        cls,
        candidates: list[dict[str, Any]],
        similarity_threshold: float,
        *,
        raw_input_records: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Group only mutually confirmed candidates deterministically without A-B-C chaining."""
        n = len(candidates)
        if n == 0:
            return []

        ordered = sorted(candidates, key=cls._canonical_sort_key)

        pair_rels: dict[tuple[int, int], tuple[str, float]] = {}
        for i in range(n):
            for j in range(i + 1, n):
                rel, conf = cls._pair_relation(ordered[i], ordered[j], similarity_threshold)
                pair_rels[(i, j)] = (rel, conf)
                pair_rels[(j, i)] = (rel, conf)

        visited = [False] * n
        output: list[dict[str, Any]] = []

        for i in range(n):
            if visited[i]:
                continue

            clique = [i]
            for j in range(i + 1, n):
                if visited[j]:
                    continue
                if all(pair_rels[(m, j)][0] == "confirmed" for m in clique):
                    clique.append(j)

            if len(clique) > 1:
                for idx in clique:
                    visited[idx] = True
                group_records = [ordered[idx] for idx in clique]
                output.append(
                    cls._build_entity_record(
                        group_records,
                        status=_STATUS_CONFIRMED,
                        action="Merge observations",
                        confidence=0.95,
                        raw_input_records=raw_input_records,
                    )
                )
                continue

            visited[i] = True
            possible_confs = [
                pair_rels[(i, j)][1]
                for j in range(n)
                if j != i and pair_rels[(i, j)][0] == "possible"
            ]
            distinct_confs = [
                pair_rels[(i, j)][1]
                for j in range(n)
                if j != i and pair_rels[(i, j)][0] == "distinct"
            ]

            if possible_confs:
                output.append(
                    cls._build_entity_record(
                        [ordered[i]],
                        status=_STATUS_POSSIBLE,
                        action="Hold for review",
                        confidence=max(possible_confs),
                        raw_input_records=raw_input_records,
                    )
                )
            else:
                conf_val = max(distinct_confs) if distinct_confs else 0.85
                output.append(
                    cls._build_entity_record(
                        [ordered[i]],
                        status=_STATUS_DISTINCT,
                        action="Keep separate",
                        confidence=conf_val,
                        raw_input_records=raw_input_records,
                    )
                )

        output.sort(key=cls._canonical_sort_key)
        return output

    def _attach_pipeline_context(
        self, records: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach an isolated copy of pipeline_context or fail loudly on collision."""
        if self.pipeline_context is None:
            return list(records)

        enriched: list[dict[str, Any]] = []
        for idx, record in enumerate(records):
            if (
                "_pipeline_context" in record
                and record["_pipeline_context"] != self.pipeline_context
            ):
                raise ValueError(
                    f"Reserved field '_pipeline_context' collision on resolved record at index {idx}"
                )
            updated = dict(record)
            updated["_pipeline_context"] = copy.deepcopy(self.pipeline_context)
            enriched.append(updated)
        return enriched

    def process(self) -> list[dict[str, Any]]:
        """Resolve duplicates without mutating inputs and write the snapshot atomically."""
        _validate_distinct_io_paths(self.input_path, self.output_path)
        if self.pipeline_context is not None:
            _validate_json_serializable(
                self.pipeline_context, label="pipeline_context"
            )

        isolated_inputs = copy.deepcopy(self.normalized_records)
        if not isolated_inputs:
            serialized_empty: list[str] = []
            self.resolved_records = []
            self._write_resolved_records(serialized_empty)
            return self.resolved_records

        base_resolved = self.resolve(isolated_inputs)
        if self.use_base_resolver_output and isinstance(base_resolved, list):
            candidates = [copy.deepcopy(r) for r in base_resolved]
        else:
            candidates = copy.deepcopy(isolated_inputs)

        policy_resolved = self._enforce_non_transitive_resolution(
            candidates,
            self.similarity_threshold,
            raw_input_records=isolated_inputs,
        )
        resolved_with_context = self._attach_pipeline_context(policy_resolved)

        serialized_lines = [
            json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            for record in resolved_with_context
        ]

        self.resolved_records = resolved_with_context
        self._write_resolved_records(serialized_lines)
        return self.resolved_records

    def _write_resolved_records(
        self, serialized_lines: Iterable[str] | None = None
    ) -> None:
        """Atomically replace the output under a run lock while serializing line by line."""
        _validate_distinct_io_paths(self.input_path, self.output_path)
        lines = (
            list(serialized_lines)
            if serialized_lines is not None
            else [
                json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
                for record in self.resolved_records
            ]
        )
        with _exclusive_output_lock(self.output_path):
            target_dir = self.output_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            _ensure_artifact_gitignore(Path(__file__).parent, self.output_path)
            _ensure_artifact_gitignore(target_dir.parent, self.output_path)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target_dir,
                    prefix=f".{self.output_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as output_file:
                    temporary_path = Path(output_file.name)
                    for line in lines:
                        output_file.write(line)
                    output_file.flush()
                    os.fsync(output_file.fileno())
                temporary_path.replace(self.output_path)
                _fsync_directory(target_dir)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_RESOLVED_RECORDS_PATH",
    "PearlLemonDeduplicator",
]