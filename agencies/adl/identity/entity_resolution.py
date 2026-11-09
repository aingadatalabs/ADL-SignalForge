"""
ADL-SignalForge Entity Resolution & Identity Layer 
Path: agencies/adl/identity/entity_resolution.py
"""

import uuid
import hashlib
import unicodedata
import logging
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class EntityResolver:
    """
    Production-grade entity resolver implementing conservative domain grouping,
    safe fingerprinting, corrected fuzzy matching upper bounds, and non-destructive provenance.
    """

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self.similarity_threshold = similarity_threshold

    @staticmethod
    def generate_name_fingerprint(name: Optional[str]) -> str:
        """Generates a robust, collision-resistant cryptographic hash fingerprint for names."""
        if not name or not isinstance(name, str):
            return "unknown_name"
        normalized = unicodedata.normalize("NFKC", name).casefold()
        cleaned_tokens = " ".join(normalized.split())
        return hashlib.sha256(cleaned_tokens.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _compute_string_similarity(str1_folded: str, str2_folded: str, threshold: float) -> float:
        """
        Computes text similarity using pre-folded strings with the mathematically 
        correct SequenceMatcher length upper-bound optimization.
        """
        if not str1_folded or not str2_folded:
            return 0.0
        
        len1, len2 = len(str1_folded), len(str2_folded)
        if len1 == 0 or len2 == 0:
            return 0.0

        min_len = min(len1, len2)
        max_possible_ratio = (2.0 * min_len) / (len1 + len2)
        
        if max_possible_ratio < threshold:
            return 0.0

        return SequenceMatcher(None, str1_folded, str2_folded).ratio()

    @staticmethod
    def generate_stable_entity_id(domain: Optional[str], name_fingerprint: str) -> str:
        """Generates a stable deterministic UUID5 based on domain or fallback fingerprint."""
        namespace = uuid.NAMESPACE_DNS
        seed_key = f"domain:{domain.lower()}" if domain else f"fingerprint:{name_fingerprint}"
        return str(uuid.uuid5(namespace, seed_key))

    @staticmethod
    def _extract_tokens(name: str) -> Set[str]:
        """Extracts significant alphanumeric tokens for blocking indexes (ignoring short stopwords)."""
        if not name:
            return set()
        stopwords = {"and", "the", "for", "with", "llc", "inc", "corp", "co", "agency", "group", "digital"}
        tokens = {t for t in name.casefold().split() if len(t) > 2}
        return tokens - stopwords

    def _merge_candidate_cluster(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merges a validated cluster of candidate observations into a unified entity record."""
        if not candidates:
            return {}

        # Sort candidates to prioritize those with verified domains or robust confidence
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (
                bool(c.get("domain")),
                len(c.get("agency_name", "")),
            ),
            reverse=True
        )

        primary = sorted_candidates[0]
        domain = primary.get("domain")
        
        # Ensure we have a valid fallback fingerprint if none provided
        name_fp = primary.get("matching_fingerprint", {}).get("name_fingerprint")
        if not name_fp:
            name_fp = self.generate_name_fingerprint(primary.get("agency_name"))

        entity_id = self.generate_stable_entity_id(domain, name_fp)

        unified_entity = {
            "entity_id": entity_id,
            "agency_name": primary.get("agency_name"),
            "website": primary.get("website"),
            "domain": domain,
            "description": primary.get("description", ""),
            "confidences": primary.get("confidences", {}),
            "source_urls": [],
            "extraction_evidence": [],
            "aliases": [],
            "observation_count": len(candidates)
        }

        all_source_urls: List[str] = []
        all_evidence: List[Dict[str, Any]] = []
        
        # Deterministic, ordered alias collection
        seen_aliases = set()
        aliases = []

        for cand in candidates:
            name = cand.get("agency_name")
            if name:
                seen_aliases.add(name)

            if cand.get("source_urls"):
                all_source_urls.extend(cand["source_urls"])

            if cand.get("extraction_evidence"):
                # Defensive copying of evidence payloads
                for ev in cand["extraction_evidence"]:
                    if isinstance(ev, dict):
                        all_evidence.append(dict(ev))

            if not unified_entity["description"] and cand.get("description"):
                unified_entity["description"] = cand.get("description")

        unified_entity["source_urls"] = list(dict.fromkeys(all_source_urls))
        unified_entity["extraction_evidence"] = all_evidence
        
        # Build clean ordered aliases excluding primary display name
        primary_name = unified_entity.get("agency_name")
        for cand in candidates:
            n = cand.get("agency_name")
            if n and n != primary_name and n not in aliases:
                aliases.append(n)

        unified_entity["aliases"] = aliases
        return unified_entity

    def resolve(self, normalized_candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Resolves candidates using conservative domain grouping and token-blocked 
        fuzzy matching with conflict detection.
        """
        domain_clusters: Dict[str, List[Dict[str, Any]]] = {}
        domainless_candidates: List[Dict[str, Any]] = []

        # Step 1: Ingest and validate incoming candidates
        for candidate in normalized_candidates:
            if not isinstance(candidate, dict):
                continue
            domain = candidate.get("domain")
            if domain:
                domain_clusters.setdefault(domain, []).append(candidate)
            else:
                domainless_candidates.append(candidate)

        resolved_entities: List[Dict[str, Any]] = []
        entity_index: List[Dict[str, Any]] = []

        # Step 2: Conservative Domain Clustering with Conflict Evaluation
        for domain, group in domain_clusters.items():
            if len(group) == 1:
                merged = self._merge_candidate_cluster(group)
            else:
                # Multiple candidates share a domain. Check name variance to avoid false merges.
                primary_name = group[0].get("agency_name", "")
                primary_folded = primary_name.casefold()
                
                sub_clusters: List[List[Dict[str, Any]]] = []
                for cand in group:
                    c_name = cand.get("agency_name", "")
                    c_folded = c_name.casefold()
                    
                    # If name is highly divergent under shared domain, treat as separate brand/division
                    similarity = self._compute_string_similarity(primary_folded, c_folded, 0.70)
                    if similarity >= 0.70 or not primary_folded:
                        # Group together under domain cluster
                        if not sub_clusters:
                            sub_clusters.append([cand])
                        else:
                            sub_clusters[0].append(cand)
                    else:
                        # Divergent brand under same domain — keep separate
                        sub_clusters.append([cand])

                for cluster in sub_clusters:
                    merged = self._merge_candidate_cluster(cluster)
                    resolved_entities.append(merged)
                    name = merged.get("agency_name", "")
                    entity_index.append({
                        "entity": merged,
                        "folded_name": name.casefold(),
                        "tokens": self._extract_tokens(name)
                    })
                continue

            name = merged.get("agency_name", "")
            resolved_entities.append(merged)
            entity_index.append({
                "entity": merged,
                "folded_name": name.casefold(),
                "tokens": self._extract_tokens(name)
            })

        # Step 3: Blocked fuzzy matching for domainless or unmatched items
        for cand in domainless_candidates:
            name = cand.get("agency_name")
            if not name:
                continue

            folded_name = name.casefold()
            cand_tokens = self._extract_tokens(name)
            
            matched = False
            best_match_idx = -1
            highest_score = 0.0

            for idx, indexed in enumerate(entity_index):
                # Token blocking check with fallback for short names
                if cand_tokens and indexed["tokens"] and not (cand_tokens & indexed["tokens"]):
                    if abs(len(folded_name) - len(indexed["folded_name"])) > 3:
                        continue

                score = self._compute_string_similarity(folded_name, indexed["folded_name"], self.similarity_threshold)
                if score > highest_score:
                    highest_score = score
                    best_match_idx = idx

            if highest_score >= self.similarity_threshold and best_match_idx != -1:
                target_entity = entity_index[best_match_idx]["entity"]
                target_entity["source_urls"] = list(dict.fromkeys(target_entity["source_urls"] + cand.get("source_urls", [])))
                
                if cand.get("extraction_evidence"):
                    for ev in cand["extraction_evidence"]:
                        if isinstance(ev, dict):
                            target_entity["extraction_evidence"].append(dict(ev))

                if name != target_entity.get("agency_name") and name not in target_entity["aliases"]:
                    target_entity["aliases"].append(name)

                target_entity["observation_count"] += 1
                matched = True

            if not matched:
                new_merged = self._merge_candidate_cluster([cand])
                resolved_entities.append(new_merged)
                entity_index.append({
                    "entity": new_merged,
                    "folded_name": folded_name,
                    "tokens": cand_tokens
                })

        return resolved_entities