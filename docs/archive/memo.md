# Gastrobrain Development Plan

### Internal AI Knowledge & Consulting System (April 2026)

## Overview

The goal is to build a system that trains AI on Gastroduce Japan’s internal knowledge base and managerial decision-making logic, allowing it to function as a “digital internal advisor.”

The system will provide two core functions:

* Instant Q&A responses using internal documents stored in NotePM
* Consulting-style recommendations for areas such as EC strategy, procurement, and logistics

---

## Why Standard RAG Is Not Enough

Conventional AI retrieval systems (RAG) mainly work by “finding semantically similar text.” This creates several major limitations:

| Challenge                                         | Problem                                                                                               |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Cannot understand relationships between documents | Unable to answer questions that span multiple documents                                               |
| Cannot learn decision-making logic                | Cannot reproduce managers’ experience-based intuition or judgment that is not explicitly written down |
| Static and non-evolving                           | Does not accumulate or reflect real decision-making experience over time                              |

---

# System Architecture: Two-Layer Structure

## 1. Knowledge Layer — GraphRAG × NotePM MCP

GraphRAG stores documents as a “relationship network (graph),” enabling the AI to answer based on contextual connections rather than isolated text fragments.

* Automatically generates graphs from NotePM using Graphify (OSS)
* Achieves approximately 71× better token efficiency compared to traditional retrieval methods
* Automatically synchronizes updates via NotePM webhooks, eliminating manual maintenance

---

## 2. Reasoning Layer — Decision Trace Database

Past decisions are recorded and accumulated in the following structure:

> Situation → Options → Key Signals Considered → Decision → Result → Thought Pattern

By collecting around 50–100 cases per domain, the AI gradually learns Gastroduce-specific “decision-making tendencies” and judgment patterns.

---

# AI Agent Workflow

### Navigator AI (Haiku)

Interprets the user’s question, determines which parts of the graph to traverse, and retrieves the relevant data.

### Answer AI (Sonnet)

Generates responses and consulting recommendations using both:

* Graph-based knowledge
* Decision trace history

---

# Technology Stack

| Role                          | Tool                                                     |
| ----------------------------- | -------------------------------------------------------- |
| Document Management           | NotePM + MCP                                             |
| Graph Generation              | Graphify (OSS)                                           |
| Graph Database                | Neo4j                                                    |
| Vector Search                 | Qdrant                                                   |
| Decision DB / Episodic Memory | PostgreSQL                                               |
| LLM                           | Claude Sonnet (response generation) + Haiku (navigation) |

---

# Roadmap

| Phase   | Timeline      | Details                                                                          |
| ------- | ------------- | -------------------------------------------------------------------------------- |
| Phase 1 | Weeks 1–2     | NotePM × Graphify integration, initial knowledge graph generation, Q&A prototype |
| Phase 2 | Weeks 2–4     | Collect decision traces from 3–5 key managers and build the reasoning layer      |
| Phase 3 | Weeks 4–6     | Implement episodic memory, company-wide rollout, establish feedback loops        |
| Phase 4 | Week 6 onward | Continuous self-improvement phase through ongoing accumulation of experience     |

---

# Expected Benefits

* Significant reduction in time spent responding to internal knowledge inquiries
* Faster onboarding for new managers and staff
* Improved EC decision-making quality based on historical success/failure patterns
* Preservation of veteran employees’ tacit knowledge as a long-term organizational asset
* Maximization of existing NotePM investment as an AI utilization platform foundation
