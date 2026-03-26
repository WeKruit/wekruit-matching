# Phase 3: LLM Enrichment - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

Anthropic LLM classification of industry, skills, company size, and sponsorship with cost controls. Every unenriched job gets classified without re-enriching unchanged jobs.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion. Key research findings:

- Use Anthropic Claude Haiku for cost-effective classification (fast, cheap, good for structured extraction)
- Content-hash gating: query only jobs where `enriched_at IS NULL` or `content_hash` changed since last enrichment
- Controlled vocabulary for industry: tech, fintech, healthtech, ecommerce, enterprise_saas, ai_ml, cybersecurity, gaming, social_media, hardware, consulting, other, unknown
- Company size values: startup, midsize, large, unknown
- Sponsorship: true, false, unknown (null)
- Skills: free-form list but validated against common programming/tech skills
- Use structured output (JSON mode) with Anthropic SDK
- Null/unknown must be first-class values — never hallucinate
- Batch processing with rate limiting via tenacity (exponential backoff on 429/5xx)
- Single API failure must not abort the entire enrichment run
- Store enriched_at timestamp to track when enrichment happened

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/models/job.py` — Job model with industry, company_size, required_skills, sponsorship fields
- `src/wekruit_matching/config.py` — Settings with ANTHROPIC_API_KEY
- `src/wekruit_matching/db/connection.py` — psycopg3 pool
- `src/wekruit_matching/db/tables.py` — jobs table with enrichment columns

### Integration Points
- Reads unenriched jobs from DB (WHERE enriched_at IS NULL)
- Updates jobs table with classification results + enriched_at timestamp
- Content hash comparison gates re-enrichment

</code_context>

<specifics>
## Specific Ideas

From spec enrichment prompt:
```
Given this job listing, extract JSON:
{
  "industry": "one of controlled vocab",
  "company_size": "startup|midsize|large",
  "skills_inferred": ["list", "of", "skills"],
  "likely_sponsors_visa": true/false
}
```

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
