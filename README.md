# ADL-SignalForge

**Company Intelligence & Lead Operations Infrastructure by Ainga Data Labs**

Turn scattered business information into organized, evidence-backed intelligence that teams can actually use.

ADL-SignalForge helps businesses reduce the manual work involved in finding companies, researching them, understanding their relevance, identifying useful contacts and moving that information into their daily operations.

Many businesses still depend on people manually searching websites, copying company details into spreadsheets, checking contact information, comparing records, researching decision-makers and transferring everything into CRMs or other systems.

This work is repetitive, difficult to maintain and expensive to repeat whenever the target market changes.

SignalForge brings these activities into a connected intelligence workflow.

Built by **Ainga Data Labs (ADL)**, it is being developed for ADL's own commercial operations first, with the intention of supporting agencies, sales teams, research organizations and businesses that depend on reliable company information.

**We help businesses spend less time collecting and organizing information and more time using it to make decisions and take action.**

---

## 1. The problem

Finding a business is relatively easy. Understanding it well enough to make a useful business decision takes considerably more work.

Consider a company trying to build a list of potential customers.

Someone must:

- Search for businesses that match the target market.
- Open individual websites and understand what each company does.
- Collect company names, websites, locations and business details.
- Determine whether multiple records refer to the same business.
- Find relevant people and establish their roles.
- Check whether available contact information is credible.
- Research services, technologies and other relevant business signals.
- Decide which companies deserve attention first.
- Transfer the information into a spreadsheet, CRM or outreach platform.
- Repeat the process when information changes or a new market is targeted.

The same problem exists beyond sales prospecting.

Research teams investigate markets. Agencies identify potential customers. Businesses compare suppliers. Investors research companies. Operations teams maintain business directories. Strategy teams monitor competitors.

Different activities, but a common problem:

**Useful business information is scattered across different sources, while the work required to collect, verify, organize and maintain it is often manual.**

The consequences include:

- Time spent researching instead of acting.
- Inconsistent company records.
- Duplicate businesses and contacts.
- Incomplete or unreliable information.
- Poor prioritization.
- Repeated research costs.
- Delayed handoffs between teams and software.
- Limited visibility into where information came from.

SignalForge is designed to reduce this operational burden.

## 2. What ADL-SignalForge does

SignalForge connects the work between discovering a business and making its information useful.

It helps organizations:

1. Find businesses that match a defined requirement.
2. Collect relevant information from configured sources.
3. Organize records into consistent company profiles.
4. Identify duplicate or conflicting company identities.
5. Enrich profiles with additional business and contact evidence.
6. Evaluate companies against specific requirements.
7. Prioritize records for review or action.
8. Deliver results to external systems and operational workflows.
9. Track processing, delivery and data quality.

The objective is not to collect the largest possible number of records.

It is to produce information that is relevant, understandable, traceable and useful.

SignalForge is not limited to generating lead lists. Lead generation is one application of a broader company intelligence and data operations system.

## 3. Built for ADL first

ADL uses SignalForge to support its own business development and identify organizations that may benefit from its services.

Ainga Data Labs provides and develops solutions around:

- Data engineering and automated pipelines.
- Company and market intelligence.
- Prospect discovery and enrichment.
- Backend engineering and API development.
- Data infrastructure and automation.
- Custom research and intelligence products.

### Initial commercial focus

ADL's first acquisition workflow targets independent B2B agencies, particularly:

- Lead-generation agencies.
- Cold outreach and outbound sales agencies.
- Demand-generation businesses.
- Appointment-setting agencies.
- Growth and performance marketing agencies with relevant data requirements.

The initial target is approximately 10 suitable agency clients.

This is a commercial objective, not an existing customer count.

SignalForge helps ADL identify suitable businesses, understand their operations, research relevant stakeholders and prepare evidence-backed commercial opportunities.

### Beyond ADL's internal operations

The same foundation can support other organizations with different information requirements.

For example:

- An agency may need a continuously refreshed list of companies matching a particular customer profile.
- A sales team may need better company and decision-maker research before approaching an account.
- A research organization may need structured information about businesses in a particular industry.
- A distributor may need to identify potential suppliers and compare their offerings.
- A business may need to monitor competitors and changes in a target market.

The business requirement determines what information is collected, how it is evaluated and where the results are delivered.

**One intelligence foundation. Different business requirements. Configurable outputs.**

---

## 4. How SignalForge works

SignalForge follows a connected workflow.

```text
             BUSINESS REQUIREMENT
                     |
                     v
                 DISCOVERY
                     |
                     v
                NORMALIZATION
                     |
                     v
               ENTITY RESOLUTION
                     |
                     v
                 ENRICHMENT
                     |
                     v
                QUALIFICATION
                     |
                     v
                   TIERING
                     |
                     v
                  PERSISTENCE
                     |
                     v
                  PUBLICATION
                     |
                     v
                   DELIVERY
                     |
                     v
             BUSINESS APPLICATIONS
                     |
                     v
                  FEEDBACK
```

Each stage has a defined responsibility.

### 4.1 Discovery

Find businesses and collect information from configured sources.

Potential sources include:

- Company websites.
- Public business directories.
- Industry directories.
- Search results and publications.
- Public professional profiles.
- Customer-provided datasets.
- Authorized third-party data providers.

Collection depends on the business requirement.

A company research workflow and an agency prospecting workflow do not necessarily need the same information.

### 4.2 Normalization

Information collected from different sources rarely arrives in the same format.

Normalization organizes it into consistent records.

This includes:

- Standardizing company names.
- Validating and canonicalizing URLs.
- Normalizing domains.
- Handling missing or malformed fields.
- Preserving original source references.
- Assigning stable internal identifiers.

This reduces the manual work of cleaning spreadsheets and comparing inconsistent records.

### 4.3 Entity resolution

Different sources may describe the same company using different names, URLs or identifiers.

SignalForge identifies whether records represent the same business.

It uses available identity evidence, compares company names and domains, identifies potential duplicates and preserves conflicting observations for review.

The objective is to avoid counting one business several times or incorrectly merging unrelated businesses.

### 4.4 Enrichment

A company name and website are rarely enough to understand an opportunity.

Enrichment adds relevant information from available company material and configured providers.

Depending on the workflow, this can include:

- Business descriptions.
- Products and services.
- Industry and market information.
- Relevant website pages.
- Decision-makers and professional roles.
- Public business contact information.
- Social and professional profiles.
- Technology observations.
- Other market-specific signals.

The enrichment process distinguishes observed information from generated or inferred information.

For example, an email address discovered on a company website is different from a generated email permutation. A domain's mail-routing configuration does not prove that an individual mailbox exists.

Every important observation should retain its source and evidence status.

### 4.5 Qualification

Not every discovered business is relevant to every requirement.

SignalForge evaluates records against a defined Ideal Customer Profile (ICP), business rule or research policy.

For ADL's initial agency workflow, qualification considers:

- Whether the company actually provides relevant B2B services.
- Whether its business model aligns with ADL's offering.
- Whether its operations suggest relevant data requirements.
- Whether useful company and stakeholder evidence is available.

The initial scoring model considers business fit, technology alignment and intent-related signals:

```text
Composite Score =
    (0.40 × Fit)
  + (0.30 × Technology Alignment)
  + (0.30 × Intent Signal)
```

This is an initial configurable model, not a universal buying-intent measurement.

A high score does not prove purchase intent. Missing information should not automatically count as negative evidence.

Qualification results should explain why a company received its score and retain supporting evidence.

### 4.6 Tiering and routing

Once evaluated, records receive a processing priority.

| Classification | Initial policy | Intended action |
|---|---|---|
| Tier 1 — Hot | Score ≥ 80 with sufficient evidence | Priority review |
| Tier 2 — Nurture | Score 50–79 | Follow-up and monitoring |
| Low fit | Score below 50 | Retain or archive |
| Pending | Incomplete information | Further enrichment |
| Manual review | Conflicting evidence | Human assessment |
| Disqualified | Confirmed exclusion | Record reason and exclude |

Thresholds are configurable and subject to validation.

Tiering determines what should happen next. It does not automatically mean a person should receive an email or message.

---

## 5. From company intelligence to business operations

Collecting information is only part of the job.

The information must reach the people and applications that use it.

SignalForge is being designed around multiple downstream consumers.

### CRM and sales systems

Structured company information can support:

- Company and contact records.
- Account segmentation.
- Qualification history.
- Account research.
- Sales preparation.
- Information synchronization.

Potential integrations include HubSpot, Salesforce and other CRM platforms.

### Prospecting and outbound systems

Relevant records can be prepared for human-reviewed prospecting workflows.

Potential uses include:

- Industry-specific prospect lists.
- Company and stakeholder research.
- Campaign segmentation.
- Contact evidence.
- Suppression and exclusion handling.

Potential destinations include Apollo, Instantly, Smartlead, Clay and other outbound platforms.

SignalForge prepares intelligence. It does not assume that every qualified business should automatically be contacted.

### Research and business intelligence

Structured outputs can support:

- Market research.
- Industry directories.
- Competitor monitoring.
- Supplier discovery.
- Company comparisons.
- Technology research.
- Commercial opportunity analysis.

### APIs and external applications

Selected information can be exposed through authenticated APIs.

Potential applications include:

- Company lookup.
- Filtered company retrieval.
- Qualification results.
- Enrichment requests.
- Dataset access.
- CRM synchronization.
- Customer dashboards.

Access controls and customer data boundaries must be implemented before these become production customer-facing services.

### Alerts and notifications

Important events can be delivered to external systems or human operators.

Examples include:

- A newly qualified high-priority company.
- An enrichment failure requiring review.
- A completed pipeline execution.
- An important change in company information.
- A failed external delivery.

This separates the creation of useful intelligence from the operational work of delivering it.

---

## 6. ADL-SignalForge delivery infrastructure

The delivery architecture is being developed around three responsibilities.

**1. API control plane**

The FastAPI application provides a controlled interface for monitoring and initiating pipeline and dispatch operations.

Planned endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/healthz` | Service health |
| GET | `/readyz` | Dependency readiness |
| POST | `/api/v1/trigger-pipeline` | Pipeline execution trigger |
| GET | `/api/v1/runs/{run_id}` | Run status |
| POST | `/api/v1/dispatch/process` | Outbox processing |
| GET | `/api/v1/dispatch/status` | Dispatch status |

Pipeline control endpoints require authentication using environment-managed API credentials.

**2. Scheduled execution**

The intended hosting environment is Render.

Scheduled execution will allow ADL to run configured intelligence and delivery operations without depending on a developer's local computer.

The initial scheduling model uses controlled API-triggered batches.

The current DuckDB storage design requires particular care around concurrent writers. Execution ownership and scheduling must preserve database consistency.

**3. Durable delivery**

SignalForge uses an outbox-oriented architecture to separate business events from external delivery.

The intended lifecycle is:

```text
Intelligence generated
        |
        v
Event persisted
        |
        v
Delivery created
        |
        v
Worker claims delivery
        |
        +------------------+
        |                  |
        v                  v
     Webhook            Alert
        |                  |
        v                  v
 CRM / Dashboard     WhatsApp / Email
        |                  |
        +--------+---------+
                 |
                 v
          Outcome recorded
                 |
          +------+------+
          |             |
          v             v
       Delivered     Retry / Review
```

Each delivery has its own outcome.

A successful webhook should not be repeated simply because a separate email or WhatsApp delivery failed.

The delivery subsystem is being designed to support:

- Safe event claiming.
- Independent destination tracking.
- Signed webhook payloads.
- Bounded processing batches.
- Retry scheduling.
- Exponential backoff.
- Idempotency protection.
- Failed-delivery handling.
- Operational logging.

Delivery guarantees must account for external provider behavior and process failures. The intended model is recoverable at-least-once delivery with deduplication, rather than an unsupported promise of exactly-once delivery.

---

## 7. Multi-client and multi-ICP direction

SignalForge is not being designed around one fixed company profile.

Different organizations have different requirements.

A B2B agency targeting supplement brands needs different information from one targeting SaaS companies. A supplier research workflow needs different qualification rules from an outbound sales workflow.

Each customer configuration should define:

- ICP and business requirements.
- Discovery sources.
- Required company attributes.
- Enrichment requirements.
- Qualification and exclusion rules.
- Scoring weights.
- Data freshness expectations.
- Output formats.
- Delivery destinations.

The initial commercial planning model allows up to five ICP configurations per agency.

With an initial target of 10 agency customers, that represents a potential 50 ICP configurations.

This is a planning objective, not a statement of current production capacity.

Customer data, execution permissions and outputs must remain appropriately separated.

---

## 8. Data quality and traceability

A company record should be useful to another person or system without requiring them to repeat the original research.

SignalForge is designed to preserve:

| Information | Purpose |
|---|---|
| Company identity | Identify the business consistently |
| Canonical domain | Connect observations to the company |
| Source records | Establish where information originated |
| Company signals | Describe relevant business observations |
| Contact evidence | Preserve stakeholder and contact information |
| Technology signals | Record observed technologies |
| Enrichment status | Show what information was obtained |
| Qualification result | Explain why the company was evaluated a certain way |
| Tier | Determine processing priority |
| Collection timestamp | Establish when information was observed |
| Update timestamp | Track changes |
| Provenance | Support verification and investigation |

The engineering principles are straightforward:

1. Preserve original evidence.
2. Distinguish missing information from failed retrieval.
3. Distinguish observed facts from inferred information.
4. Avoid unsupported contact verification claims.
5. Keep identity decisions explainable.
6. Preserve conflicting information.
7. Track data freshness.
8. Validate records before downstream delivery.

**The value is not just having company information. It is knowing what the information means, where it came from and whether it can be trusted.**

---

## 9. Current engineering foundation

ADL-SignalForge is being developed as a modular Python application with clear responsibilities across discovery, processing, persistence and delivery.

The repository currently includes the following areas:

```text
ADL-SignalForge/
│
├── ADL/
│   └── Internal acquisition workflow
│
├── dispatch/
│   ├── outbox.py
│   ├── webhook_client.py
│   └── notifier.py
│
├── storage/
│   ├── models/
│   ├── duckdb_client.py
│   ├── jsonl_store.py
│   ├── parquet_sink.py
│   ├── pipeline_runner.py
│   └── repository.py
│
├── scripts/
│   └── worker.py
│
├── .github/
│   └── workflows/
│       └── ci.yml
│
├── main.py
├── requirements.txt
├── render.yaml
├── test_signalforge.py
└── README.md
```

This illustrates the current working repository structure, not a claim that all components are production-ready.

### Persistence and execution

The persistence layer is responsible for storing operational events, managing analytical data and providing controlled repository access.

Current engineering work includes:

- JSONL event persistence.
- DuckDB database access.
- Parquet dataset output.
- Repository-level persistence operations.
- Pipeline execution lifecycle management.
- Shared pipeline data contracts.
- Provenance and record accounting.

### Enrichment and qualification

The wider application is being developed around reusable enrichment contracts, company identity resolution and configurable qualification.

The initial qualification design separates deterministic rules, scoring, tiering and orchestration.

### Delivery and notifications

The dispatch layer separates outbox management, webhook communication and notification providers.

The intended adapters include:

- External webhooks for dashboards and CRMs.
- WhatsApp notifications for important operational alerts.
- Resend email notifications.

### API and deployment

The API entrypoint is prepared for authenticated execution and Render deployment.

The intended operating model includes scheduled pipeline triggers, bounded dispatch operations and health monitoring.

### Testing and code quality

A GitHub Actions workflow has been added to the working repository.

The visible workflow configuration includes:

- Python 3.11 environment setup.
- Dependency installation.
- Ruff code-quality checks.
- Application and worker import checks.
- Conditional pytest execution.

Test discovery and lint enforcement still require alignment and verification.

---

## 10. Current development status

SignalForge is under active development.

The following distinction is important: reviewing a component, defining its contract and successfully operating it in production are three different milestones.

| Area | Current position |
|---|---|
| ADL-first commercial direction | Defined |
| Initial agency ICP | Defined |
| Discovery and normalization | Architecture established; implementation under continued review |
| Entity resolution | Implementation and reliability review |
| Shared enrichment contracts | Defined and reviewed |
| Enrichment components | Architecture and code review |
| Qualification rules | Designed and reviewed |
| Scoring and tiering | Initial policies defined; validation required |
| Pipeline orchestration | Implementation reviewed; contract and recovery work remains |
| JSONL persistence | Implementation reviewed; failure and shutdown issues identified |
| DuckDB and repository | Implementation reviewed; integration verification required |
| Parquet publication | Implementation reviewed; end-to-end verification required |
| Outbox and webhook delivery | Architecture and component review |
| WhatsApp and email adapters | Integration design and code review |
| Secure FastAPI entrypoint | Initial implementation reviewed; execution integration pending |
| Dispatch worker | Initial structure reviewed; full behavior not yet verified |
| GitHub Actions CI | Workflow configuration present; verification and test discovery alignment required |
| Render deployment and cron | Planned; deployment and execution verification pending |
| Multi-client execution | Progressive implementation planned |

No external delivery integration or production execution capability should be considered operational until its complete workflow has been tested.

--

## 11. How ADL measures value

SignalForge should be evaluated by the work it helps businesses complete, not simply the amount of information it collects.

### Business outcomes

- Relevant companies identified.
- Research time reduced.
- Suitable opportunities discovered.
- Relevant stakeholders identified.
- Qualified records accepted by customers.
- Commercial conversations generated.
- Paying customers acquired.
- Repeat business and retention.

### Information quality

- Company identity accuracy.
- Duplicate rate.
- Enrichment completeness.
- Contact evidence quality.
- Source traceability.
- Qualification precision.
- Data freshness.

### Operational performance

- Processing time per company.
- Cost per usable record.
- Cost per qualified opportunity.
- Failed processing rate.
- Delivery success rate.
- Retry and recovery performance.
- Time required to configure a new ICP.

A smaller number of accurate, relevant records can be more valuable than thousands of incomplete or poorly researched companies.

---

## 12. Engineering principles

1. **Solve real operational problems.** Reduce repetitive research, data collection and administrative work.
2. **Business requirements come first.** Technology serves the information requirement.
3. **Build for ADL first.** Validate the internal acquisition workflow before scaling to external customers.
4. **Evidence over assumptions.** Preserve the source behind important observations.
5. **Identity before enrichment.** Understand which company a record represents.
6. **Missing is not negative.** Incomplete evidence should remain distinguishable from confirmed exclusion.
7. **Separate responsibilities.** Keep discovery, enrichment, qualification, persistence and delivery independent.
8. **Make decisions explainable.** Qualification should show why a business was prioritized.
9. **Design for different requirements.** Support configurable ICPs and outputs.
10. **Delivery is part of the product.** Information must reach the people and systems that need it.
11. **Reliability before scale.** Handle failures, retries and duplicate execution deliberately.
12. **Documentation reflects reality.** Distinguish implemented, reviewed, tested and planned capabilities.

---

## Maintained by Ainga Data Labs

**Project:** ADL-SignalForge  
**Organization:** Ainga Data Labs  
**Website:** https://aingadatalabs.com  
**GitHub:** https://github.com/aingadatalabs  
**Contact:** hello@aingadatalabs.com

*ADL-SignalForge — less time searching, organizing and repeating research; more time making informed business decisions.*