# Reading list: state of the art

Papers in this folder, ranked by how much they matter for the dissertation. Read them in this order.

**Objectives** (from the proposal):

- **O1**: multi-agent architecture for Quality Engineering (test generation, static analysis, vulnerability detection)
- **O2**: local-first / hybrid SLM–LLM strategies (cost, privacy)
- **O3**: integration and validation in real DevOps pipelines

Last updated: 2026-11-01

---

## Priority 1: read in full

### 1. LLM-Based Multi-Agent Systems for Software Engineering: Literature Review, Vision, and the Road Ahead
- **File:** [3712003.pdf](3712003.pdf) · He, Treude & Lo · *TOSEM 2025* · **O1**
- **Why:** The main survey for multi-agent systems in software engineering. It maps these systems across the software development lifecycle and lists open research gaps.
- **Take from it:** The research agenda section. Position your contribution against the gaps it names.
- [x] Read

### 2. A Comprehensive Survey on Benchmarks and Solutions in Software Engineering of LLM-Empowered Agentic System
- **File:** [2510.09721v3.pdf](2510.09721v3.pdf) · Guo et al. · *arXiv 2025* · **O1, O3** · proposal ref [3]
- **Why:** Sorts solutions into prompt-based, fine-tuning-based and agent-based, and links 50+ benchmarks to the solutions evaluated on them.
- **Take from it:** Candidate benchmarks and metrics for the evaluation phase (Phase 4).
- [x] Read

### 3. Small Language Models are the Future of Agentic AI
- **File:** [2506.02153v3.pdf](2506.02153v3.pdf) · Belcak et al. (NVIDIA) · *arXiv 2025* · **O2** · proposal ref [2]
- **Why:** The main argument for local-first agents. Its "heterogeneous agentic systems", where one agent calls several models, are the hybrid setup in your proposal.
- **Take from it:** The working definition of an SLM (a model that fits on a consumer device), the barriers to adoption, and the LLM-to-SLM conversion algorithm. It argues a position and has no experiments.
- [x] Read

### 4. Automated Unit Test Improvement using Large Language Models at Meta
- **File:** [3663529.3663839.pdf](3663529.3663839.pdf) · Alshahwan et al. · *FSE Companion 2024 (Industry)* · **O1, O3**
- **Why:** LLM-based QE deployed in a real industrial pipeline. TestGen-LLM keeps a generated test only if it passes filters (it builds, passes reliably and increases coverage). This is the same idea as your analyzers rejecting findings that no tool result backs up.
- **Take from it:** The filter design, the deployment story (test-a-thons on Instagram and Facebook), and how they report results (75% built, 57% passed reliably, 25% increased coverage).
- [x] Read

### 5. FlexFL: Flexible and Effective Fault Localization With Open-Source Large Language Models
- **File:** [FlexFL_Flexible_and_Effective_Fault_Localization_With_Open-Source_Large_Language_Models.pdf](FlexFL_Flexible_and_Effective_Fault_Localization_With_Open-Source_Large_Language_Models.pdf) · Xu et al. · *TSE 2025* · **O1, O2**
- **Why:** The closest match to your prototype's pattern. Traditional techniques narrow the candidates first, and agents on open-source LLMs (Llama3-8B) then refine them. It is motivated by data privacy.
- **Take from it:** How they make agents work with models that have no native function calling. This applies directly to the local models you run through Ollama.
- [x] Read

### 6. Small Models, Big Tasks: An Exploratory Empirical Study on Small Language Models for Function Calling
- **File:** [3756681.3757001.pdf](3756681.3757001.pdf) · Kavathekar et al. · *ACM 2025* · **O2**
- **Why:** Finds that SLMs struggle to stick to the required output format when calling functions. That is what you saw with qwen3:4b, and it is the reason the workflow versions exist.
- **Take from it:** The comparison of zero-shot, few-shot and fine-tuning, and how they measure latency and memory on an edge device. You can reuse that method for O2.
- [x] Read

### 7. An Empirical Evaluation of Using Large Language Models for Automated Unit Test Generation (TestPilot)
- **File:** [An_Empirical_Evaluation_of_Using_Large_Language_Models_for_Automated_Unit_Test_Generation.pdf](An_Empirical_Evaluation_of_Using_Large_Language_Models_for_Automated_Unit_Test_Generation.pdf) · Schäfer et al. · *TSE 2024* · **O1, O2**
- **Why:** The reference study for LLM test generation. When a test fails, it re-prompts the model with the failing test and the error message, which is a feedback loop like yours.
- **Take from it:** The ablation of prompt components and the coverage metrics. It also finds that the open model StarCoder does worse, which is evidence on model size.
- [x] Read

### 8. MetaGPT: Meta Programming for a Multi-Agent Collaborative Framework
- **File:** [2308.00352v7.pdf](2308.00352v7.pdf) · Hong et al. · *ICLR 2024* · **O1**
- **Why:** Gives agents standardized operating procedures (SOPs) and structured intermediate outputs, so errors don't snowball as agents hand work to each other.
- **Take from it:** The reasoning for structured hand-offs between agents. It supports your workflow design with JSON answers.
- [x] Read

### 9. ChatDev: Communicative Agents for Software Development
- **File:** [2024.acl-long.810.pdf](2024.acl-long.810.pdf) · Qian et al. · *ACL 2024* · **O1**
- **Why:** The standard multi-agent software development framework, with role-based agents including a tester.
- **Take from it:** The chat chain and its "communicative dehallucination" method. It is a baseline to compare your architecture against.
- [x] Read

---

## Priority 2: read the relevant sections

| # | Paper | File | Venue | Obj. | Why |
|---|---|---|---|---|---|
| 10 | Toprani & Madisetti, *LLM Agentic Workflow for Automated Vulnerability Detection and Remediation in IaC* | [pdf](LLM_Agentic_Workflow_for_Automated_Vulnerability_Detection_and_Remediation_in_Infrastructure-as-Code.pdf) | IEEE Access 2025 | O1, O3 | Uses multiple agents plus retrieval to catch misconfigurations that static tools miss, integrated into CI/CD. Check whether they compare numerically against a static-tool baseline. |
| 11 | Zhou, Zhang & Lo, *LLM for Vulnerability Detection: Emerging Results and Future Directions* | [pdf](3639476.3639762.pdf) | ICSE-NIER 2024 | O1 | Short early results for the vulnerability detection agent. Only tests GPT-3.5 and GPT-4, so pair it with the critical studies in the last section. |
| 12 | Pornprasit & Tantithamthavorn, *Fine-tuning and Prompt Engineering for LLM-based Code Review Automation* | [pdf](1-s2.0-S0950584924001289-main.pdf) | IST 2024 | O1, O2 | Code review as a QE task, framed around organisations with limited budgets. Includes an open model (Magicoder). |
| 13 | Sharma & Mehta, *Small Language Models for Agentic Systems: A Survey* | [pdf](2510.03847v1.pdf) | arXiv 2025 | O2 | SLM-first routing with an LLM fallback, verifiers, and metrics you could reuse (cost per successful task, schema validity). Not peer-reviewed: use it as a map and cite the primary sources it points to. |
| 14 | Becattini et al., *SALLMA: A Software Architecture for LLM-Based Multi-Agent Systems* | [pdf](SALLMA_A_Software_Architecture_for_LLM-Based_Multi-Agent_Systems.pdf) | SATrends 2025 (workshop) | O1 | Architecture reference for Phase 2: an operational layer and a knowledge layer, deployed with Docker and Kubernetes. A vision paper; the authors say its benefits are unvalidated. |
| 15 | Zhang et al., *LLM-Based Agents for Automating the Enhancement of User Story Quality* | [pdf](978-3-031-61154-4_8.pdf) | XP 2024 | O3 | A different task, but an industrial evaluation with practitioners (Austrian Post). A model for the STIC case study. |

---

## Priority 3: on topic, but don't rely on their numbers

Skim these for ideas. Don't cite their results as evidence.

| # | Paper | File | Concern |
|---|---|---|---|
| 16 | Naqvi et al., *The Rise of Agentic Testing* (arXiv 2026) | [pdf](2601.02454v1.pdf) | Exactly your theme (agents that generate, execute and review tests in CI/CD), but a preprint whose "up to 60%" claim comes from unnamed microservice apps. Read its evaluation before citing any figures. |
| 17 | Salem et al., *AI-Driven Continuous Integration* (FMEC 2025) | [pdf](AI-Driven_Continuous_Integration_Automating_Code_Review_and_Deployment_with_LLMs.pdf) | Its 30% and 40% improvement figures seem to come from surveys and interviews, not experiments. |
| 18 | Nagvekar, *Agentic AI-Driven CI/CD Pipelines* (ICTBIG 2025) | [pdf](Agentic_AI-Driven_CI_CD_Pipelines_for_Autonomous_Software_Delivery.pdf) | A 4-page conceptual framework by one author, with no evaluation. |
| 19 | Joshi, *A Review of Generative AI and DevOps Pipelines* (SSRN 2025) | [pdf](ssrn-5290005.pdf) | Not peer-reviewed. Use its reference list to find other papers, and don't cite it. |

---

## Added later

### 20. IRIS: LLM-Assisted Static Analysis for Detecting Security Vulnerabilities
- **File:** [ICLR-2025-iris-llm-assisted-static-analysis-for-detecting-security-vulnerabilities-Paper-Conference.pdf](ICLR-2025-iris-llm-assisted-static-analysis-for-detecting-security-vulnerabilities-Paper-Conference.pdf) · Li, Dutta & Naik · *ICLR 2025* · **O1, O2**
- **Why:** The only paper in the folder on static analysis combined with LLMs. The LLM labels which library APIs are taint sources and sinks, CodeQL traces the data flow, and the LLM then filters out false positives.
- **Take from it:** The split between what the tool does and what the model decides, the false-positive filter, and the finding that small models make that filter worse.
- [x] Read

---

## Not in the folder yet

Ordered by priority. The first group would go into Priority 1.

**Priority 1:**

- [ ] Liu et al., *Large Language Model-Based Agents for Software Engineering: A Survey* (arXiv 2409.02977). This is **proposal ref [1]**.
- [ ] Xia et al., *Agentless: Demystifying LLM-based Software Engineering Agents*. Shows a fixed pipeline can beat autonomous agents, which supports your workflow version.
- [ ] Huang et al., *Large Language Models Cannot Self-Correct Reasoning Yet* (ICLR 2024). Shows models need external feedback to correct themselves, which justifies tool-backed validation.
- [ ] Ding et al., *Hybrid LLM: Cost-Efficient and Quality-Aware Query Routing* (ICLR 2024). Routing between a small and a large model, for O2.

**Priority 2:**

- [ ] Yang et al., *SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering* (NeurIPS 2024). The agent side of the agent vs. workflow comparison.
- [ ] Ding et al., *Vulnerability Detection with Code Language Models: How Far Are We?* (ICSE 2025). A reality check for the vulnerability detection agent.
- [ ] Ullah et al., *LLMs Cannot Reliably Identify and Reason About Security Vulnerabilities (Yet?)* (IEEE S&P 2024). Same purpose.
- [ ] Chen, Zaharia & Zou, *FrugalGPT*, and Ong et al., *RouteLLM*. Cost-aware model cascades and routing.
- [ ] Hou et al., *Large Language Models for Software Engineering: A Systematic Literature Review* (TOSEM 2024).
- [ ] Wang et al., *Software Testing with Large Language Models: Survey, Landscape, and Vision* (TSE 2024).

**For the framing chapter (QA → QE, DevOps):**

- [ ] Fitzgerald & Stol, *Continuous Software Engineering: A Roadmap and Agenda* (JSS 2017).
- [ ] Leite et al., *A Survey of DevOps Concepts and Challenges* (ACM Computing Surveys 2019).

**Only if the accessibility and link analyzers stay in the dissertation:**

- [ ] Vigo, Brown & Conway, *Benchmarking Web Accessibility Evaluation Tools* (W4A 2013).
- [ ] W3C, *Web Content Accessibility Guidelines (WCAG) 2.2*.
