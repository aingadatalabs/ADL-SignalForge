# ADL-SignalForge

**Ainga Data Labs — Agency Acquisition & Multi-ICP Lead Intelligence Platform**

ADL-SignalForge is the lead-discovery, prospect-research, and ICP intelligence platform developed by Ainga Data Labs (ADL).

The platform is designed to help ADL acquire lead-generation agency clients and subsequently provide the configurable data collection, prospect discovery, qualification, and contact intelligence pipelines those agencies need.

The immediate objective is to identify and acquire approximately **10 suitable lead-generation agencies** before scaling the platform. Each agency-specific directory will contain a pipeline designed around that agency's business and support up to five relevant ideal customer profiles (ICPs).

ADL's own acquisition pipeline lives in the `ADL/` directory. It is responsible for identifying businesses that may need ADL's data infrastructure, intelligence products, and backend engineering services.

FrankenSurf is the selected web-ingestion layer for retrieving source content. SignalForge provides the business logic around that ingestion: source configuration, ICP-specific extraction, qualification, normalization, contact intelligence, and prospect preparation.

The objective is not to collect the largest possible number of leads. It is to build a repeatable system that identifies commercially relevant prospects and delivers useful, evidence-backed intelligence to paying clients.

---

## 1. Project objectives

SignalForge addresses two connected business objectives.

### 1.1 Acquire ADL's first agency clients

The initial commercial target is approximately 10 lead-generation agencies that could benefit from ADL's data engineering and prospect intelligence capabilities.

The acquisition pipeline should help answer:

- Which agencies match ADL's target customer profile?
- What industries, businesses, and customer segments do they serve?
- Which ICPs do their lead-generation services target?
- What observable evidence suggests that an agency could benefit from better data collection or prospect intelligence?
- Who are the relevant business contacts?
- What evidence supports a personalized outreach approach?
- Which agencies have progressed from discovery to a qualified commercial opportunity?

The acquisition process should prioritize agency fit, evidence of potential need, contact quality, and commercial relevance rather than raw lead volume.

### 1.2 Build reusable lead-generation infrastructure

Once an agency becomes a client, SignalForge should support the development and operation of ICP-specific prospecting pipelines for that agency.

Each agency can have its own:

- Target ICP definitions.
- Discovery sources.
- Source-specific ingestion configuration.
- Extraction and normalization rules.
- Qualification criteria.
- Contact discovery requirements.
- Enrichment providers.
- Output contracts and delivery requirements.

Each agency pipeline is intended to support **up to five ICPs**. This is a design limit for the initial operating model, not a requirement that every agency must use all five.

The platform should reuse common infrastructure wherever appropriate without forcing every agency to share identical business rules.

---

## 2. Operating model

SignalForge follows an agency-first operating model.

```text
                     ADL-SIGNALFORGE
                             |
              Agency Discovery & Qualification
                             |
              ADL Acquisition Pipeline
                       ADL/
                             |
              Qualified Agency Prospects
                             |
                Outreach & Conversion
                             |
                  Agency Client Acquired
                             |
                Agency-Specific Pipeline
                             |
              +--------------+--------------+
              |              |              |
            ICP 1          ICP 2          ICP 3
              |              |              |
         Discovery       Discovery       Discovery
              |              |              |
         Qualification   Qualification   Qualification
              |              |              |
              +--------------+--------------+
                             |
                 ICP 4 and ICP 5
                  Where Required
                             |
                 Contact Intelligence
                             |
                 Qualified Lead Records
                             |
                 Client-Ready Delivery
                             |
                 Feedback & Refinement
```

This represents the intended operating model. The complete multi-agency orchestration and client-delivery workflow should be treated as planned until implemented and tested.

### The two sides of SignalForge

**ADL acquisition:** Find and convert agencies that can become paying clients of ADL.

**Client delivery:** Build and operate prospect intelligence pipelines that help those agencies serve their own customers.

These are related but distinct workflows. ADL's own acquisition criteria should not be confused with the ICP criteria of an agency's customers.

---

## 3. Repository architecture

The repository uses agency-specific directories to separate client requirements and ICP implementations.

The `ADL/` directory is reserved for ADL's own acquisition pipeline. External agency directories will be named after the corresponding agencies as they are identified and onboarded.

```text
ADL-SignalForge/
├── ADL/
│   └── run_pipeline.py
│
├── <agency_01>/
│   └── ...
│
├── <agency_02>/
│   └── ...
│
├── ...
│
├── <agency_10>/
│   └── ...
│
├── README.md
├── test_signalforge.py
├── .gitignore
└── .venv/
```

The directory names above illustrate the target structure. Agency directories and their internal implementations should be added as the project progresses.

The local virtual environment is development infrastructure and should not be committed to version control.

### Agency directory conventions

Each agency directory should represent one agency and its specific lead-generation requirements.

As implementations mature, an agency directory may contain:

- Agency-specific configuration.
- ICP definitions.
- Discovery and ingestion adapters.
- Extraction and normalization logic.
- Qualification rules.
- Contact discovery and enrichment integrations.
- Output schemas.
- Tests and fixtures.
- Documentation describing the agency's pipeline.

The precise internal structure should be standardized when common patterns emerge from the first implementations.

Do not create empty directories or placeholder modules and then document them as operational capabilities.

---

## 4. FrankenSurf ingestion architecture

FrankenSurf is the selected ingestion layer for SignalForge.

Its role is to retrieve source content through an appropriate available reading tool, validate whether the retrieved page is useful, and report failures when the requested content cannot be obtained correctly.

SignalForge builds on that capability with source-specific business logic.

### 4.1 Ingestion responsibilities

The ingestion layer should support:

- Reading configured public web pages.
- Retrieving page text or Markdown for downstream processing.
- Handling different page-rendering and retrieval requirements.
- Distinguishing usable page content from challenge pages, login walls, and other unsuccessful outcomes.
- Capturing relevant retrieval failures.
- Supporting controlled batching and retries.
- Passing source content and retrieval metadata to downstream extractors.

FrankenSurf's available interfaces include synchronous and asynchronous reading, batching, search, site mapping, and crawling. Which interfaces are used should depend on the needs of each pipeline.

### 4.2 SignalForge adapter responsibilities

The SignalForge application layer remains responsible for:

- Defining target sources and collection rules.
- Choosing the pages relevant to a particular ICP.
- Configuring execution limits and concurrency.
- Calling FrankenSurf through a consistent adapter interface.
- Converting retrieved content into structured records.
- Extracting business-specific signals.
- Recording source provenance and collection outcomes.
- Handling incomplete results and downstream validation.

FrankenSurf retrieves content; SignalForge determines what that content means for the business problem being solved.

### 4.3 Extraction and qualification

Retrieved content should be passed to the relevant ICP-specific extractor.

For example, an agency targeting Shopify supplement brands may need product counts, product pricing, brand information, and contact channels.

An agency targeting SaaS companies may need different signals, such as product category, target market, pricing model, or publicly documented technical requirements.

These are examples of possible ICP-specific requirements, not claims that all corresponding extractors are currently implemented.

### 4.4 Integration boundary

FrankenSurf is the selected ingestion technology, but the status of the integration must be established from the actual code.

The existence of a FrankenSurf dependency or a successful standalone read does not prove that the complete SignalForge pipeline uses it end to end.

Integration should be considered complete only when the relevant adapter, pipeline invocation, error handling, output contract, and tests have been implemented and verified.

Scrapling is not the selected ingestion layer for the revised architecture.

---

## 5. ADL acquisition pipeline

The `ADL/` directory contains ADL's own client-acquisition workflow.

Its purpose is to discover businesses that may become customers of Ainga Data Labs.

Potential commercial offerings include:

- Lead intelligence and ICP discovery.
- Data collection and pipeline engineering.
- Backend engineering and API development.
- Company and market intelligence datasets.
- Custom data infrastructure.
- Automated prospect research and qualification.

The pipeline should identify prospects based on observable business characteristics and evidence of potential need.

### Acquisition workflow

The intended process is:

1. Discover businesses matching ADL's target agency profile.
2. Collect relevant company information.
3. Extract observable commercial and operational signals.
4. Qualify and prioritize agency prospects.
5. Discover relevant business contacts.
6. Enrich contact records where appropriate.
7. Produce research-backed prospect records.
8. Prepare personalized outreach for human review.
9. Track conversations and qualification outcomes.
10. Refine the acquisition criteria based on commercial results.

The actual implementation status of each stage should be maintained separately from this target workflow.

### Acquisition qualification

Agency qualification should consider several independent dimensions:

| Dimension | Question |
|---|---|
| Agency fit | Does the business provide lead-generation or a closely related service? |
| ICP complexity | Does its work involve identifiable, repeatable prospect segments? |
| Data requirements | Could its workflow benefit from structured discovery, extraction, or enrichment? |
| Commercial relevance | Is there a credible reason for ADL's services to be useful? |
| Contact confidence | Is the proposed decision-maker's identity and role sufficiently supported? |
| Evidence quality | Can the qualification decision be traced to relevant observations? |
| Acquisition status | Has the prospect been discovered, reviewed, contacted, or converted? |

A high score should not be interpreted as proof of buying intent. Scores should be explainable through observable evidence.

---

## 6. Agency-specific multi-ICP pipelines

Each acquired agency may require a different prospecting strategy.

SignalForge therefore treats an agency's pipeline as a configurable collection of ICP-specific discovery workflows.

### Initial design constraint

- Approximately 10 external agency clients are the initial acquisition target.
- Each agency pipeline supports up to five ICPs.
- ICPs are defined according to the agency's actual customer acquisition requirements.
- ADL's own pipeline remains separate under `ADL/`.

The maximum planned capacity is therefore approximately 50 agency-specific ICP configurations across 10 agencies, if every agency uses all five ICP slots.

This is a planning capacity, not a claim that 50 pipelines currently exist or need to be built immediately.

### Example

Suppose an acquired agency serves several types of businesses.

Its directory could eventually contain:

```text
<agency_name>/
├── config/
│   └── icps.yaml
├── discovery/
├── extraction/
├── qualification/
├── contacts/
├── outputs/
└── tests/
```

This is an illustrative target layout. Its adoption should follow the actual requirements of the first agency implementations.

An ICP configuration might specify:

- ICP identifier and description.
- Inclusion and exclusion criteria.
- Permitted discovery sources.
- Required company attributes.
- Signals to extract.
- Qualification rules.
- Contact requirements.
- Data freshness requirements.
- Output fields.
- Collection and execution limits.

A common configuration model should be introduced when the first agency pipelines reveal stable patterns worth standardizing.

### Avoid premature generalization

Not every difference between agencies should become a new shared abstraction.

Initially, the priority is to acquire customers and understand their requirements. Reusable interfaces should emerge from validated workflows rather than speculative platform complexity.

---

## 7. Prospect data and evidence

SignalForge should produce structured prospect records that are useful for research, qualification, and downstream lead delivery.

A future shared record model may include:

| Field | Purpose |
|---|---|
| `company_id` | Internal company identifier |
| `company_name` | Business name |
| `domain` | Canonical company domain |
| `agency_id` | Agency responsible for the pipeline |
| `icp_id` | ICP used to discover or qualify the prospect |
| `source` | Discovery or ingestion source |
| `source_url` | URL supporting an observation |
| `signals` | Structured evidence extracted from the source |
| `qualification` | ICP fit and scoring details |
| `contact_candidates` | Relevant discovered or enriched contacts |
| `enrichment_provider` | External provider, where applicable |
| `collected_at` | Collection timestamp |
| `last_verified_at` | Last actual verification time, if available |
| `record_status` | Processing, qualification, review, or delivery status |

This is a proposed data contract, not a claim that the current scripts already emit all these fields.

### Data quality principles

- Preserve source provenance wherever possible.
- Distinguish missing information from failed collection.
- Do not treat unverified contact details as verified.
- Retain evidence supporting important qualification decisions.
- Normalize company domains and identifiers consistently.
- Detect duplicates within and across sources.
- Avoid overwriting conflicting observations without retaining useful provenance.
- Keep agency-specific data access and delivery boundaries explicit.
- Track data freshness when it materially affects the usefulness of a lead.

The storage model should reflect the actual data contracts and operating requirements rather than being introduced solely for architectural completeness.

---

## 8. External integrations

SignalForge may use external services for ingestion, proxy infrastructure, contact discovery, or enrichment.

Each integration should have a defined responsibility and a clear failure boundary.

### FrankenSurf

**Purpose:** Source-page retrieval and ingestion.

**Configuration:** Use the package, runtime, or server configuration supported by the selected deployment mode.

### Oxylabs

**Purpose:** Proxy infrastructure where required by a pipeline's authorized collection strategy.

**Configuration:** Provider credentials and endpoint settings should be supplied through environment variables or an appropriate secret manager.

A proxy is not a guarantee of successful access. Its use must remain consistent with applicable laws, source restrictions, and provider terms.

### Hunter.io

**Purpose:** Professional contact discovery and enrichment where appropriate.

**Configuration:** Supply the API key through a protected environment variable.

Enrichment results should retain provider provenance and should not automatically be treated as proof that a person is the correct decision-maker.

### Additional providers

Other contact, company, or data enrichment providers may be added when a validated client requirement justifies them.

Provider integrations should use explicit timeouts, controlled retries, quota awareness, and useful error reporting.

API keys and credentials must never be committed to source control.

---

## 9. Development environment

The project is developed in Python using the local WSL/Ubuntu environment.

FrankenSurf's documented package installation requires Python 3.12 or newer. Verify the active interpreter before integrating the dependency.

Check the current Python version:

```bash
python --version
```

Create a virtual environment if one is not already configured:

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
```

Install FrankenSurf according to the requirements of the chosen integration mode.

For direct Python usage:

```bash
python -m pip install "frankensurf[mcp]"
frankensurf install
```

The `mcp` extra is appropriate when MCP support is needed. If SignalForge only requires direct Python calls, select the smallest supported installation that provides the required capabilities.

The commands above illustrate installation rather than a verified, complete SignalForge environment setup.

Maintain a project dependency manifest and pin dependencies for reproducible installations once the current runtime and integrations are confirmed.

### Environment configuration

Keep credentials in a local `.env` file or another supported secret-management mechanism.

For example:

```dotenv
# Add only variables required by the implemented integrations.

FRANKENSURF_API_KEY=replace_with_a_local_api_key_if_required

OXYLABS_USER=replace_with_provider_username
OXYLABS_PASS=replace_with_provider_password

HUNTER_API_KEY=replace_with_hunter_api_key
```

These are illustrative configuration names. The Frankensurf API key is relevant to a configured server deployment; it is not necessarily required for direct local Python usage.

Only configure integrations that the relevant pipeline actually uses.

Never commit populated `.env` files, API credentials, proxy credentials, or private prospect datasets.

---

## 10. Execution and orchestration

The repository currently exposes `ADL/run_pipeline.py` as an acquisition-pipeline entry point.

The actual execution contract should be documented alongside its implementation.

A pipeline runner should eventually provide:

- Explicit configuration loading.
- Defined execution stages.
- Controlled collection limits.
- Clear success and failure reporting.
- Consistent output locations.
- Useful execution summaries.
- Recovery behavior for failed stages.
- Appropriate exit codes for automation.
- Separation between discovery, qualification, and enrichment.

Agency-specific pipelines should have similarly clear entry points as they are introduced.

### Execution status

A command should be documented as a supported run instruction only after it has been verified against the current repository.

The presence of `run_pipeline.py` alone does not establish that every acquisition, ingestion, enrichment, or outreach stage is implemented.

---

## 11. Testing and reliability

SignalForge must establish that collected information is correct and that failures do not silently produce misleading prospect records.

The repository includes `test_signalforge.py`. Its current coverage should be determined by inspecting and running the tests rather than inferred from the filename.

### Recommended test coverage

**Unit tests**

- Signal extraction and normalization.
- ICP inclusion and exclusion rules.
- Qualification scoring.
- Domain normalization and deduplication.
- Contact record validation.
- Missing and malformed source data.

**Ingestion tests**

- Successful retrieval.
- Incorrect or irrelevant page content.
- Empty responses.
- Challenge pages and access restrictions.
- Timeouts and transient failures.
- Invalid source URLs.
- Unexpected content structures.

**Pipeline integration tests**

- Configuration loading.
- Ingestion-to-extraction handoffs.
- Extraction-to-qualification handoffs.
- Output schema validation.
- Partial failure handling.
- Repeat execution and duplicate prevention.

**Agency-specific tests**

- Correct application of the agency's ICP definitions.
- Separation of different ICP outputs.
- Enforcement of collection limits.
- Compliance with the expected client output contract.

Use fixtures and controlled test sources wherever possible. Tests should not depend on unrestricted live collection or external API availability unless the integration test explicitly requires it.

---

## 12. Security and data governance

SignalForge handles business intelligence and may process personal contact information. Security and responsible data handling must be built into the operating model.

Required controls include:

- Protected credentials and secret rotation.
- Restricted access to prospect datasets.
- Appropriate logging and sensitive-data redaction.
- Explicit collection limits.
- Source and provider policy compliance.
- Data provenance and freshness tracking.
- Appropriate retention and deletion procedures.
- Contact correction and suppression workflows.
- Human review of prospect qualification and outreach.
- Clear separation of agency-specific client data.

Collect only information relevant to a defined business purpose. Respect applicable laws, access restrictions, contractual obligations, and provider terms.

Automated outreach should not be considered operational until appropriate review, consent or other applicable legal-basis requirements, suppression controls, and delivery safeguards have been implemented.

---

## 13. Roadmap

The roadmap prioritizes commercial validation before broad platform expansion.

### Phase 1 — Validate ADL's agency acquisition model

- [ ] Define the target profile for suitable lead-generation agencies.
- [ ] Establish the initial agency discovery and qualification criteria.
- [ ] Implement and verify the ADL acquisition pipeline.
- [ ] Validate FrankenSurf ingestion against a controlled set of relevant sources.
- [ ] Produce structured, evidence-backed agency prospect records.
- [ ] Establish a repeatable prospect review and outreach workflow.
- [ ] Acquire the first agency client.
- [ ] Refine qualification and delivery requirements using actual conversations.

### Phase 2 — Acquire approximately 10 agencies

- [ ] Identify and qualify suitable agency prospects.
- [ ] Track each agency through the acquisition process.
- [ ] Convert qualified prospects into paying clients.
- [ ] Record each client's ICP and data requirements.
- [ ] Document the expected lead output and delivery criteria.
- [ ] Validate the commercial value of the service before scaling.

The target is approximately 10 acquired agencies, not merely 10 agencies discovered or contacted.

### Phase 3 — Build agency-specific ICP pipelines

- [ ] Create the first agency-specific pipeline from a validated client requirement.
- [ ] Implement its source discovery and ingestion adapters.
- [ ] Extract and normalize its ICP-specific signals.
- [ ] Add its qualification rules and output contract.
- [ ] Validate lead quality with the client.
- [ ] Expand to additional ICPs where justified.
- [ ] Support up to five ICPs per agency.
- [ ] Identify common components worth extracting into shared infrastructure.

### Phase 4 — Standardize the shared platform

- [ ] Define stable company, contact, signal, and qualification schemas.
- [ ] Standardize FrankenSurf integration interfaces.
- [ ] Extract reusable configuration and normalization services.
- [ ] Implement reliable execution and retry behavior.
- [ ] Establish cross-source deduplication.
- [ ] Add structured observability and automated tests.
- [ ] Introduce shared persistence where justified by operational requirements.
- [ ] Establish agency-level data access and isolation.

### Phase 5 — Intelligence-driven outreach and operations

- [ ] Generate prospect research briefs from supporting evidence.
- [ ] Introduce AI-assisted, individually tailored outreach drafts.
- [ ] Add human approval and factuality checks.
- [ ] Implement scheduling and delivery controls where appropriate.
- [ ] Track replies, qualification outcomes, and opt-outs.
- [ ] Improve discovery and qualification using measured outcomes.
- [ ] Evaluate production hosting, monitoring, cost, and reliability.

Roadmap completion should be based on tested implementations and measurable outcomes rather than directory creation or documentation changes.

---

## 14. Success metrics

SignalForge should measure both commercial performance and data quality.

### Commercial metrics

- Qualified agencies discovered.
- Agencies contacted.
- Positive agency conversations.
- Agencies progressing to commercial discussions.
- Paying agency clients acquired.
- Time from discovery to conversion.
- Client retention and repeat business.

### Pipeline quality metrics

- Successful and usable page retrieval rate.
- Extraction completeness.
- ICP qualification precision.
- Duplicate prospect rate.
- Contact validity and confidence.
- Source freshness.
- Cost per qualified prospect.
- Time required to onboard a new ICP.
- Client acceptance of delivered leads.

Metrics must be defined consistently before being compared across agencies.

A large number of collected records is not evidence of a successful pipeline if those records are irrelevant, stale, duplicated, or commercially unusable.

---

## 15. Current project status

ADL-SignalForge is evolving from an initial lead-intelligence implementation into an agency-first, multi-ICP platform.

The current repository screenshot establishes the presence of:

- `ADL/run_pipeline.py`
- `README.md`
- `test_signalforge.py`

The screenshot does not establish the implementation status or test coverage of every pipeline stage.

The revised architecture establishes the following direction:

| Capability | Intended role | Status |
|---|---|---|
| ADL acquisition pipeline | Acquire ADL's own clients | Entry point present; implementation requires verification |
| FrankenSurf ingestion | Retrieve and validate source content | Selected technology; end-to-end integration requires verification |
| Agency-specific pipelines | Serve each acquired agency | Progressive implementation |
| Up to five ICPs per agency | Support client-specific discovery requirements | Design constraint |
| Shared qualification and normalization | Reuse common intelligence services | Introduce as implementations justify |
| Contact discovery and enrichment | Improve prospect usability | Integrate as required and verify per pipeline |
| AI-assisted outreach | Prepare personalized, evidence-backed outreach | Future capability unless implemented |
| Unified orchestration | Coordinate multi-agency execution | Future capability unless implemented |

Implementation status should be updated whenever a capability is completed and tested.

---

## 16. Engineering principles

SignalForge development follows these principles:

1. **Commercial validation before scale.** Acquire and serve real clients before expanding the platform unnecessarily.
2. **Evidence over volume.** Prioritize relevant, traceable prospect intelligence.
3. **FrankenSurf for ingestion.** Keep retrieval separate from ICP-specific business logic.
4. **One agency, one boundary.** Keep client-specific requirements and data appropriately separated.
5. **Up to five ICPs per agency.** Expand within a clear operating constraint.
6. **Reusable infrastructure where justified.** Standardize patterns demonstrated by working pipelines.
7. **Explicit failure states.** Never confuse unsuccessful retrieval with a genuine absence of data.
8. **Security by design.** Protect credentials, contact information, and client datasets.
9. **Human oversight.** Keep qualification and outreach reviewable.
10. **Documentation follows implementation.** Distinguish working capabilities from architectural plans.

---

## Maintained by Ainga Data Labs

**Project:** ADL-SignalForge  
**Organization:** Ainga Data Labs  
**Website:** https://aingadatalabs.com  
**GitHub organization:** https://github.com/aingadatalabs  
**Contact:** hello@aingadatalabs.com