# Customer QA Demo Knowledge Base

This small demo corpus is assembled from public U.S. government and USPS
customer-help pages. It is for retrieval experiments, not a merchant's
actual return policy. Each document keeps its canonical source URL so the
answer layer can expose provenance.

Sources:

- FTC, [Online Shopping](https://consumer.ftc.gov/online-shopping)
- FTC, [Solving Problems With a Business](https://consumer.ftc.gov/articles/solving-problems-business-returns-refunds-and-other-resolutions)
- USPS, [Where is my package?](https://faq.usps.com/articles/Knowledge/Where-is-my-package)
- USPS, [Change of Address - The Basics](https://faq.usps.com/articles/Knowledge/Change-of-Address-The-Basics)

Index for the demo tenant:

```bash
cd platform/agent-server
AGENT_RAG_BACKEND=memory \
  python -m app.rag.index.indexer knowledge/customer-qa \
  --tenant qa-eval-tenant --collection customer-faq
```

The default runtime `rag_search` collection remains unchanged until the
tenant-specific collection wiring is enabled in the agent pack.
