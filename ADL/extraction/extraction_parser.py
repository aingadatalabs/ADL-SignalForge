"""
ADL-SignalForge Extraction Module (Production Grade V5.1 - Hardened & Cleaned)
Path: ADL/extraction/extraction_parser.py
"""

from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
import re
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional, Set, Any
from ADL.extraction.normalizers import normalize_domain, is_valid_agency_url, is_blocked_domain

# Combined single-pattern regex to avoid TypeError in re.compile()
IGNORED_HEADING_PATTERNS = re.compile(
    r'^(?:(?:why|our|frequently|final|table|conclusion|how|'
    r'overview|methodology|faq|summary|what|key|pros|cons)\b|'
    r'\d+[\.\)]\s*(?:table|faq|conclusion|summary|methodology)\b)',
    re.IGNORECASE
)


def clean_agency_heading(text: str) -> Optional[str]:
    """Safely cleans agency heading text without truncating valid international/brand names."""
    if not text or not isinstance(text, str):
        return None
    cleaned = re.sub(r'\s+', ' ', text).strip()
    # Strip leading list ranking numbers like "1." or "2)"
    cleaned = re.sub(r'^\d+[\.)]\s*', '', cleaned).strip()
    
    if not cleaned or len(cleaned) < 2 or len(cleaned) > 100:
        return None
    if IGNORED_HEADING_PATTERNS.search(cleaned):
        return None
    return cleaned


def generate_candidate_fingerprint(source_url: str, heading_text: str) -> str:
    """Generates a deterministic extraction fingerprint for provenance tracking."""
    raw_str = f"{source_url}:{heading_text.lower().strip()}"
    return f"cand-{hashlib.sha256(raw_str.encode('utf-8')).hexdigest()[:16]}"


def score_and_extract_website_candidates(links_found: List[Any], base_url: str) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Evaluates all outbound links in a container, returning ranked website candidates 
    with structured selection reasons and evidence. Requires a positive score threshold (> 0).
    """
    candidates: List[Dict[str, Any]] = []

    for link in links_found:
        if not hasattr(link, 'get'):
            continue
        href_val = link.get('href')
        if not href_val:
            continue
        href: str = href_val[0] if isinstance(href_val, list) else str(href_val)
        
        if not is_valid_agency_url(href, base_url):
            continue

        absolute_url: str = urljoin(base_url, href)
        domain: Optional[str] = normalize_domain(absolute_url)
        if not domain or is_blocked_domain(domain):
            continue

        anchor_text: str = link.get_text(" ", strip=True) if hasattr(link, 'get_text') else ""
        anchor_lower = anchor_text.lower()
        score = 0
        reason = "default_outbound_link"

        # Positive scoring for official website signals
        if any(kw in anchor_lower for kw in ['website', 'visit', 'official', 'site', 'homepage']):
            score += 5
            reason = "explicit_official_anchor"
        elif 'http' not in anchor_lower and len(anchor_lower) > 2:
            score += 2
            reason = "clean_anchor_text"
        else:
            score += 1

        # Penalize directories, reviews, and editorial links
        if any(kw in anchor_lower for kw in ['read more', 'learn more', 'blog', 'article', 'review', 'clutch', 'g2', 'capterra']):
            score -= 4
            reason = "directory_or_editorial_penalty"

        candidates.append({
            "url": absolute_url,
            "canonical_domain": domain,
            "anchor_text": anchor_text,
            "score": score,
            "selection_reason": reason
        })

    # Sort candidates by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Filter for a genuine positive minimum threshold (> 0)
    valid_candidates = [c for c in candidates if c["score"] > 0]

    best_url = valid_candidates[0]["url"] if valid_candidates else None
    best_domain = valid_candidates[0]["canonical_domain"] if valid_candidates else None

    return valid_candidates, best_url, best_domain


def extract_agencies_from_html(html_content: str, source_url: str) -> List[Dict[str, Any]]:
    """
    Production-grade Parser V5.1: Uncouples extraction from deduplication, retains all 
    evidence-backed candidates, uses safe timezone-aware timestamps, and normalizes element classes.
    """
    try:
        soup = BeautifulSoup(html_content, 'lxml')
    except Exception:
        soup = BeautifulSoup(html_content, 'html.parser')

    extracted_agencies: List[Dict[str, Any]] = []

    # Target candidate container heuristic
    agency_cards = soup.find_all(['div', 'section', 'article'], class_=re.compile(r'agency|card|item|listing|provider', re.IGNORECASE))
    
    candidates: List[Tuple[Tag, Optional[Tag]]] = []
    seen_headings: Set[Tag] = set()

    for card in agency_cards:
        if hasattr(card, 'find'):
            heading = card.find(['h2', 'h3', 'h4'])
            if heading and heading not in seen_headings:
                seen_headings.add(heading)
                candidates.append((heading, card))

    if not candidates:
        for h in soup.find_all(['h2', 'h3']):
            if h:
                candidates.append((h, None))

    for heading, container in candidates:
        raw_heading_text = heading.get_text(strip=True)
        agency_name = clean_agency_heading(raw_heading_text)
        
        if not agency_name:
            continue

        description_parts: List[str] = []
        links_pool: List[Any] = []

        if container:
            for el in container.find_all(['p', 'li']):
                if hasattr(el, 'get_text'):
                    text = el.get_text(" ", strip=True)
                    if text and text != raw_heading_text and text not in description_parts:
                        description_parts.append(text)
            links_pool = container.find_all('a', href=True)
        else:
            current_node = heading.find_next_sibling()
            search_nodes = [heading]
            while current_node:
                if hasattr(current_node, 'name'):
                    if current_node.name in ['h1', 'h2', 'h3']:
                        break
                    search_nodes.append(current_node)
                    sub_els = current_node.find_all(['p', 'li'], recursive=True) if current_node.name in ['div', 'section'] else [current_node]
                    for el in sub_els:
                        if hasattr(el, 'name') and el.name in ['p', 'li']:
                            text = el.get_text(" ", strip=True)
                            if text and text != raw_heading_text and text not in description_parts:
                                description_parts.append(text)
                current_node = current_node.find_next_sibling()
            
            for node in search_nodes:
                if hasattr(node, 'find_all'):
                    links_pool.extend(node.find_all('a', href=True))

        description = " ".join(description_parts)[:1200].strip()

        # Website candidate evaluation
        website_candidates, best_url, canonical_domain = score_and_extract_website_candidates(links_pool, source_url)

        # Granular Confidence Scoring
        name_confidence = "high" if len(agency_name) >= 3 else "medium"
        website_confidence = "high" if best_url and any(c["score"] >= 5 for c in website_candidates) else ("medium" if best_url else "low")

        candidate_id = generate_candidate_fingerprint(source_url, raw_heading_text)

        # Safely normalize container class attribute (can be string or list in BS4)
        raw_classes = container.get("class", []) if container else []
        classes_list = raw_classes.split() if isinstance(raw_classes, str) else list(raw_classes)

        # Build V5 Schema compliant extraction record
        extracted_agencies.append({
            "candidate_id": candidate_id,
            "agency_name": agency_name,
            "raw_heading": raw_heading_text,
            "website_candidates": website_candidates,
            "selected_website": best_url,
            "domain": canonical_domain,
            "description": description,
            "services": [],
            "source_urls": [source_url],
            "confidence": {
                "name_extraction_confidence": name_confidence,
                "website_association_confidence": website_confidence
            },
            "extraction_evidence": {
                "parser_version": "v5.1",
                "source_url": source_url,
                "heading": {"text": raw_heading_text, "tag": heading.name},
                "container": {"tag": container.name if container else "none", "classes": classes_list},
                "total_links_evaluated": len(links_pool)
            },
            "qualification_status": "pending_review",
            "identity_status": "unresolved",
            "discovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        })

    return extracted_agencies