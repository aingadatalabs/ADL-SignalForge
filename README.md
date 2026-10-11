# ADL SignalForge

**Ainga Data Labs — Unified ICP Discovery & Lead Intelligence Platform**

ADL SignalForge is the lead-intelligence and prospect research system behind [Ainga Data Labs](https://aingadatalabs.com). It combines configurable data collection, company-level signal extraction, ICP qualification, public contact discovery, and external contact enrichment to identify businesses that may benefit from ADL's data infrastructure, intelligence products, and backend engineering services.

SignalForge is being developed as a modular, multi-ICP platform. Its first vertical is the **Shopify Supplements Intelligence Pipeline (SSIP)**, which targets supplement and wellness e-commerce brands and transforms publicly accessible storefront data into qualified, research-ready business prospects.

The long-term objective is to consolidate ADL's individual ICP pipelines into a shared intelligence system: multiple discovery engines, common enrichment and qualification services, a unified prospect data layer, and an AI-assisted outreach workflow.

> **Current status:** SSIP's core discovery, signal extraction, contact discovery, and enrichment workflow is implemented. The remaining SSIP milestone is the AI-assisted cold-email drafting and scheduling workflow. Additional ICP-specific discovery pipelines and the unified orchestration layer are planned extensions, not yet represented as completed capabilities.

---

## 1. Platform objectives

SignalForge is designed to answer four questions:

1. **Who should ADL target?** Discover businesses matching defined ideal customer profiles.
2. **Why are they relevant?** Extract observable company, product, commercial, and technical signals that support qualification.
3. **Who should ADL contact?** Discover public contact channels and enrich candidate decision-makers using external providers.
4. **What should ADL do next?** Produce actionable prospect records that support human-reviewed, personalized outreach and future campaign automation.

The core principle is that lead generation should begin with a business question, not indiscriminate scraping. Every discovery engine should collect data that supports a defined ICP, qualification rule, or commercial hypothesis.

## 2. Architecture overview

SignalForge follows a modular pipeline architecture. Individual ICP engines own source discovery and domain-specific signal extraction, while shared services handle normalization, deduplication, contact enrichment, qualification, persistence, and downstream outreach preparation.

```text
                         ADL SIGNALFORGE
                                |
                  ICP Discovery / Source Adapters
                                |
             +------------------+------------------+
             |                  |                  |
       SSIP — Shopify       Creator ICPs       Future ICPs
       Supplements          Planned            Planned
             |
       Catalog Collection
             |
       Signal Extraction & Normalization
             |
       Company Deduplication & Qualification
             |
       Public Contact Discovery
             |
       External Contact Enrichment
             |
       Unified Prospect Records
             |
       Outreach Preparation & Review
             |
       AI-Assisted Email Drafting
             |
       Scheduling & Campaign Execution
             |
       Response Tracking & Feedback
```

The diagram represents the target platform architecture. Not every shared service or downstream stage is currently implemented as a unified service.

### Architectural principles

- **Modular discovery:** Each ICP has its own source adapters, collection rules, and qualification criteria.
- **Reusable infrastructure:** Proxy configuration, retry handling, normalization, deduplication, and enrichment should not be reimplemented independently for every ICP.
- **Evidence-based qualification:** Scores should be traceable to observable signals rather than unexplained rankings.
- **Explicit data lineage:** Prospect records should retain their source, collection timestamp, and enrichment provenance wherever available.
- **Resumable execution:** Long-running collection jobs should support controlled retries and recovery from partial failures.
- **Separation of concerns:** Discovery, qualification, contact enrichment, email generation, and delivery are distinct stages.
- **Human oversight:** AI-generated outreach should remain reviewable, with suppression and opt-out controls enforced before sending.
- **Incremental expansion:** New ICPs should plug into shared interfaces rather than creating isolated end-to-end systems.

---

## 3. Current implementation: SSIP

The Shopify Supplements Intelligence Pipeline is SignalForge's first ICP-specific discovery engine.

It targets supplement and wellness brands operating Shopify storefronts. The pipeline collects storefront catalog data, derives company-level commercial signals, discovers publicly listed contact details, and uses Hunter.io to identify potential decision-makers.

### 3.1 Storefront discovery and collection

The current collector targets a configured list of Shopify storefronts and requests catalog data through Shopify-style product endpoints.

The collection workflow uses asynchronous HTTP requests, proxy routing through Oxylabs, and structured outputs for downstream processing.

Collection responsibilities include:

- Fetching product catalog data from configured storefronts.
- Extracting product and variant information where available.
- Recording collection outcomes.
- Handling unsuccessful requests and access restrictions.
- Passing collected records into the signal-extraction stage.

The current implementation uses Oxylabs credentials supplied through environment variables. Proxy availability does not guarantee access to a target, and all collection must remain within applicable provider terms, site restrictions, and legal requirements.

### 3.2 Company signal extraction

The parser converts raw storefront records into structured, qualification-ready lead records.

Signals currently include:

- Store domain and brand identity.
- Successful or unsuccessful collection status.
- Product catalog size.
- Product and variant pricing.
- Vendor and product-tag information where available.
- Calculated lead score.
- Assigned ICP tier.

These signals help prioritize prospects for further research. They are indicators of potential commercial relevance, not proof of revenue, purchasing authority, or demand for ADL's services.

### 3.3 Public contact discovery

The contact-discovery stage inspects selected public pages on successfully collected storefronts, including homepages and conventional contact or about pages.

It extracts available:

- Email addresses.
- Telephone numbers.
- Social profile links.

The resulting records are intended to support prospect research and subsequent contact enrichment. Extracted contact information should be treated as unverified until validated.

### 3.4 External contact enrichment

SignalForge integrates Hunter.io Domain Search to identify potential professional contacts associated with prospect domains.

The enrichment stage consumes previously collected contact records and produces a final enriched outreach dataset.

Hunter.io is an external dependency. Results, coverage, and available decision-maker information depend on the provider's data, API access, quotas, and response status.

### 3.5 Outreach automation — remaining milestone

The next SSIP milestone is an AI-assisted cold-email workflow that consumes qualified, enriched prospect records.

The planned workflow includes:

1. Selecting prospects using ICP, score, and evidence-based qualification rules.
2. Building a company-specific research brief from available signals.
3. Generating individually tailored email drafts.
4. Connecting each email claim to supporting prospect evidence.
5. Reviewing drafts for accuracy, relevance, and deliverability risks.
6. Scheduling approved messages within configured campaign limits.
7. Recording delivery outcomes, replies, opt-outs, and suppression events.
8. Feeding campaign outcomes back into qualification and messaging decisions.

**Status:** The discovery and enrichment stages are implemented; the AI drafting and scheduling workflow remains to be completed. Automated sending, response tracking, and campaign optimization should not be considered operational until implemented and tested.

---

## 4. Current SSIP processing stages

The existing scripts run sequentially and exchange intermediate JSON and CSV files.

| Stage | Module | Responsibility | Output |
|---|---|---|---|
| 1 | `supplements_brand.py` | Shopify-style catalog collection | `store_leads.json` |
| 2 | `parser.py` | Signal extraction, scoring, and ICP classification | `enrichment_ready_leads.csv` |
| 3 | `contact_scraper.py` | Public email, phone, and social discovery | `scraped_contact_leads.csv` |
| 4 | `enrichment_api.py` | Hunter.io contact enrichment | `final_enriched_outreach_leads.csv` |
| 5 | Planned | AI-assisted drafting and scheduling | To be defined |

The current workflow uses filesystem-based handoffs rather than a single production orchestration service. The generated files are intermediate pipeline artifacts and should be stored outside version control when they contain prospect or personal data.

---

## 5. Lead scoring and qualification

The existing SSIP parser uses heuristic scoring to prioritize storefronts.

### Current scoring rules

| Observable signal | Score adjustment |
|---|---:|
| Starting score for a successfully collected store | 50 |
| At least 30 products | +10 |
| At least 100 products | +20 instead |
| At least 200 products | +30 instead |
| Average variant price of at least 20 | +10 |
| Average variant price of at least 40 | +20 instead |

The score is capped at 100. Price thresholds use the units provided by the source data; they should not be interpreted as a normalized currency comparison until currency handling is explicitly implemented.

The current ICP tiers are assigned separately:

- **Enterprise Tier 1:** 150 or more products.
- **Mid-Market Tier 2:** 50 or more products.
- **Boutique / SMB Tier 3:** Fewer than 50 products.
- **Unreachable / blocked:** Collection unsuccessful.

The tiering and scoring logic are preliminary heuristics. Catalog size and displayed prices alone do not establish company size, financial capacity, buying intent, or product-market fit.

### Future qualification model

As additional ICPs are integrated, SignalForge should distinguish between:

- **ICP fit:** How closely a prospect matches the target customer definition.
- **Commercial potential:** Whether available evidence suggests a meaningful opportunity.
- **Contact confidence:** How well the identity and role of a proposed contact are supported.
- **Timing or intent:** Whether recent, verifiable evidence indicates a relevant need.
- **Data confidence:** The completeness, freshness, and reliability of the underlying records.

These dimensions should remain independently inspectable. A unified score can be introduced later without discarding the evidence behind it.

---

## 6. Scrapling and source-adapter strategy

SignalForge's wider lead-generation roadmap includes integrating Scrapling as a reusable scraping framework for suitable source adapters.

The intention is to support multiple source-specific collectors without coupling the entire platform to one extraction strategy.

Future adapters may target publicly accessible business websites, creator platforms, directories, and other sources relevant to defined ADL ICPs.

### Adapter responsibilities

Each adapter should eventually expose a consistent interface for:

- Accepting target configuration and execution limits.
- Discovering source records.
- Extracting source-specific fields.
- Normalizing results into a shared internal schema.
- Recording provenance and collection status.
- Handling transient failures and retryable errors.
- Respecting authorization, applicable access restrictions, and provider limits.

Scrapling should be used where its capabilities fit the source and access requirements. It does not replace source-specific business logic, contact verification, deduplication, or ICP qualification.

**Implementation boundary:** Scrapling-based multi-source orchestration should be documented as complete only after the relevant adapters and shared interfaces have actually been integrated and tested. The existing SSIP catalog and contact scripts remain the reference implementation for their current workflow.

---

## 7. Oxylabs, Hunter.io, and external integrations

SignalForge separates external infrastructure and enrichment dependencies from business logic through environment-based configuration.

| Integration | Purpose | Configuration |
|---|---|---|
| Oxylabs | Proxy-routed collection where configured | `OXYLABS_USER`, `OXYLABS_PASS`, `OXYLABS_HOST`, `OXYLABS_PORT` |
| Hunter.io | Domain-based professional contact discovery | `HUNTER_API_KEY` |
| Additional enrichment provider | Potential future contact verification or company enrichment | Provider-specific configuration, not yet standardized |

Oxylabs is a transport and access-infrastructure dependency, not a guarantee that requests will succeed. Hunter.io supplies external enrichment results, not verified evidence that every returned person is the right buyer.

Additional enrichment providers should be integrated behind clear interfaces with explicit timeouts, failure handling, quota awareness, and provenance. API keys and provider-specific secrets must never be embedded in source code or written to committed logs.

---

## 8. Unified ADL ICP Intelligence — target architecture

The long-term goal is to consolidate ADL's lead-generation pipelines into a common intelligence layer.

Instead of treating SSIP, creator discovery, and future technical or business prospecting as unrelated projects, SignalForge will provide shared infrastructure for discovery, qualification, and actionable prospect preparation.

### Planned ICP families

| ICP family | Discovery objective | Status |
|---|---|---|
| Shopify supplements and wellness | Identify e-commerce brands suitable for SSIP-related intelligence offerings | Core pipeline implemented |
| Creator economy and talent management | Discover and qualify creators for relevant business-development workflows | Planned integration |
| Data and backend engineering | Identify businesses with observable data infrastructure or backend needs | Planned |
| AI and data infrastructure | Discover relevant companies and signals for ADL's data and AI engineering offerings | Planned |
| Additional verticals | Add configurable ICP definitions and source adapters as commercial evidence supports them | Future expansion |

These are planned integration targets, not claims that all collectors or qualification models already exist.

### Shared intelligence capabilities

The intended unified layer should provide:

- A canonical company and prospect schema.
- Domain and entity resolution.
- Cross-source deduplication.
- Evidence-backed company signals.
- ICP-specific qualification rules.
- Contact discovery and enrichment.
- Data freshness and provenance tracking.
- Reusable execution and retry policies.
- A unified prospect store and query interface.
- Outreach-ready exports and, eventually, campaign orchestration.

The result should be a single ADL prospect-intelligence system that can support multiple commercial offerings without forcing every ICP to share identical collection logic or scoring criteria.

---

## 9. Technology and dependencies

The current SSIP workflow uses Python and the following dependencies:

- **Python 3.10 or newer:** Pipeline implementation.
- **httpx:** Asynchronous HTTP requests.
- **python-dotenv:** Local environment configuration.
- **Beautiful Soup 4:** HTML parsing and public-page extraction.
- **Oxylabs:** Configured proxy infrastructure.
- **Hunter.io API:** External professional contact enrichment.

Scrapling is part of the wider source-adapter integration direction and should be declared as a dependency when the relevant modules are incorporated into the executable workflow.

The dependencies above describe the current SSIP scripts, not a finalized dependency manifest for every future SignalForge component. A production release should maintain a pinned, reproducible dependency specification and a documented setup process.

---

## 10. Configuration and local setup

Run the existing SSIP pipeline from the repository root.

Create and activate a virtual environment:

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install httpx python-dotenv beautifulsoup4
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install httpx python-dotenv beautifulsoup4
```

Create a local `.env` file at the repository root:

```dotenv
OXYLABS_USER=your_oxylabs_username
OXYLABS_PASS=your_oxylabs_password
OXYLABS_HOST=pr.oxylabs.io
OXYLABS_PORT=7777

HUNTER_API_KEY=your_hunter_api_key
```

The host and port shown above are the current configuration defaults. Use the endpoint and authentication settings assigned to your Oxylabs account.

Do not commit `.env` files, API credentials, proxy credentials, or private prospect datasets.

### Run the SSIP pipeline

```bash
python SSIP/supplements_brands/supplements_brand.py
python SSIP/supplements_brands/parser.py
python SSIP/supplements_brands/contact_scraper.py
python SSIP/supplements_brands/enrichment_api.py
```

Run the stages in order when starting from a fresh collection. Individual stages can be run separately when their expected input files already exist.

---

## 11. Data contracts and storage

The current pipeline exchanges intermediate JSON and CSV files under:

```text
SSIP/supplements_brands/outputs/
```

The intended unified platform should move toward explicit, versioned data contracts shared by all ICP engines.

A canonical prospect record should accommodate fields such as:

| Field | Purpose |
|---|---|
| `company_id` | Stable internal company identifier |
| `company_name` | Normalized business name |
| `domain` | Canonical company domain |
| `icp_type` | Source ICP or prospect category |
| `source` | Discovery source or adapter |
| `source_url` | URL supporting the observation |
| `signals` | Structured evidence extracted from sources |
| `qualification` | ICP fit and scoring details |
| `contact_candidates` | Discovered or enriched professional contacts |
| `enrichment_provider` | Provider responsible for an enrichment result |
| `collection_timestamp` | Time of collection |
| `last_verified_at` | Time of the last actual verification, if available |
| `record_status` | Processing, qualification, or review state |

This is a target schema, not a claim that every field exists in the current CSV outputs.

The unified model should preserve multiple observations and contact candidates rather than overwriting conflicting information without provenance. Missing values must remain distinguishable from negative findings, failed requests, and unverified data.

---

## 12. Reliability, security, and operational readiness

Before SignalForge is treated as a production-grade, multi-ICP service, the following controls should be implemented and verified.

### Reliability

- Automated tests for parsing, scoring, normalization, and deduplication.
- Integration tests against controlled fixtures and a small authorized target set.
- Explicit request timeouts, bounded retries, and exponential backoff where appropriate.
- Per-source rate limits and concurrency controls.
- Structured logs and actionable error reporting.
- Idempotent processing and recovery from interrupted runs.
- Monitoring for collection success rates, schema drift, and enrichment failures.

### Security

- Environment-based secret management.
- Redaction of credentials, personal data, and sensitive request details from logs.
- Restricted access to generated prospect datasets.
- Dependency pinning and vulnerability review.
- Separate development and production configuration.
- Documented credential rotation and incident-response procedures.

### Data governance

- Respect applicable laws, source terms, and provider policies.
- Collect only information necessary for a defined business purpose.
- Track the origin and freshness of contact information.
- Establish retention, correction, and deletion procedures.
- Maintain suppression lists and honor opt-out requests.
- Require appropriate review before outreach automation is enabled.

These controls are production-readiness requirements; their inclusion in this document does not imply that all have already been implemented.

---

## 13. Roadmap

### Phase 1 — SSIP foundation

- [x] Shopify-style catalog collection.
- [x] Company signal extraction and heuristic qualification.
- [x] Public contact discovery.
- [x] Hunter.io-based contact enrichment.
- [ ] Complete AI-assisted cold-email drafting.
- [ ] Implement approved-message scheduling and campaign controls.

### Phase 2 — Shared SignalForge infrastructure

- [ ] Establish canonical company, contact, and signal schemas.
- [ ] Extract reusable collection, retry, normalization, and deduplication services.
- [ ] Integrate Scrapling-based source adapters where appropriate.
- [ ] Standardize configuration, dependency management, and execution interfaces.
- [ ] Add automated tests, structured observability, and recovery controls.

### Phase 3 — Multi-ICP discovery

- [ ] Integrate the creator-intelligence pipeline.
- [ ] Add data and backend engineering prospect discovery.
- [ ] Add further ICP-specific source adapters and qualification rules.
- [ ] Consolidate prospect records and resolve duplicate entities across ICPs.

### Phase 4 — Intelligence-driven outreach

- [ ] Generate personalized outreach briefs from supporting evidence.
- [ ] Add AI-assisted email drafting with factuality and relevance checks.
- [ ] Introduce review and approval workflows.
- [ ] Add scheduling, sending controls, suppression enforcement, and campaign records.
- [ ] Measure replies and outcomes to improve qualification and messaging.

### Phase 5 — Production operations

- [ ] Introduce a unified orchestrator and durable execution state.
- [ ] Evaluate persistent storage and migrations for unified prospect data.
- [ ] Add scheduled jobs, health checks, metrics, and alerting.
- [ ] Establish operational runbooks, access controls, and retention policies.
- [ ] Validate reliability, provider costs, and performance under representative workloads.

Priorities may change as ADL validates customer demand and learns which ICPs produce qualified conversations and commercial opportunities.

---

## 14. Repository structure

The following reflects the current SSIP layout and the intended direction of the broader repository. Planned directories are architectural targets and should be added as their implementations become available.

```text
ADL-SignalForge/
├── SSIP/
│   └── supplements_brands/
│       ├── supplements_brand.py
│       ├── parser.py
│       ├── contact_scraper.py
│       ├── enrichment_api.py
│       └── outputs/
│
├── signalforge/                 # Planned shared platform package
│   ├── adapters/                # Shared source-adapter contracts
│   ├── discovery/               # Discovery orchestration
│   ├── normalization/           # Canonical data contracts
│   ├── qualification/           # ICP rules and scoring
│   ├── enrichment/              # Shared provider integrations
│   ├── storage/                 # Unified persistence interfaces
│   └── orchestration/           # Execution coordination
│
├── icps/                        # Planned ICP-specific engines
│   ├── supplements/
│   ├── creators/
│   └── data_backend/
│
├── outreach/                    # Planned AI-assisted outreach
│   ├── research/
│   ├── drafting/
│   └── scheduling/
│
├── tests/                       # Automated test suite
├── docs/                        # Architecture and operational docs
├── .env.example                 # Safe configuration template
├── .gitignore
├── requirements.txt             # Or equivalent pinned dependency file
└── README.md
```

---

## 15. Troubleshooting

**Missing Oxylabs credentials**

Check that `OXYLABS_USER` and `OXYLABS_PASS` are set in the process environment or local `.env` file and that the configured endpoint matches your account.

**Missing intermediate files**

Run the collection, parsing, contact discovery, and enrichment stages in order. Confirm that each stage writes its expected output under `SSIP/supplements_brands/outputs/`.

**Blocked or rate-limited storefronts**

Check the recorded request outcome, endpoint availability, source restrictions, and provider limits. Reduce request pressure where necessary. A proxy is not a guarantee of access.

**No public contact details**

A store may not publish email addresses on the pages inspected by the current collector. Missing results should not be treated as proof that no contact exists.

**No Hunter.io results**

Verify the API key, quota, provider response, and domain quality. Keep unsuccessful enrichment distinct from a confirmed absence of relevant contacts.

**Partial or stale data**

Inspect source timestamps and stage outputs before re-running. Where the current workflow does not provide durable checkpoints, preserve necessary intermediate artifacts securely before recovery attempts.

---

## 16. Project status and contribution principles

ADL SignalForge is an evolving engineering system. The SSIP pipeline establishes the initial end-to-end discovery and enrichment workflow; the wider multi-ICP architecture and outreach automation remain under active development.

Engineering changes should prioritize:

1. Correctness and traceability over raw lead volume.
2. Reusable components over duplicated pipeline logic.
3. Observable failure states over silent data loss.
4. Evidence-backed qualification over arbitrary scores.
5. Secure, compliant data handling over indiscriminate collection.
6. Verified implementation status over aspirational documentation.

**Maintained by Ainga Data Labs**

Website: https://aingadatalabs.com

GitHub: https://github.com/aingadatalabs

Contact: hello@aingadatalabs.com
