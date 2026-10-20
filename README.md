# MSc in AI Capstone #6: Agentic AI Systems – Adaptive HKDSE ICT Practice Tutor

Repository: https://github.com/PHIacademy/autonomous-agentic-workflows.git

A multi-agent practice tutor for HKDSE ICT Paper 1A (multiple-choice), built with [smolagents](https://github.com/huggingface/smolagents).

## Overview

- **Task:** Design, implement and evaluate an agentic AI workflow (orchestrator plus specialised workers, with tools, memory and safeguards)
- **Domain:** HKDSE ICT Paper 1A past-paper practice (adaptive question selection, grading, explanation, difficulty analysis)
- **Dataset:** 560 real HKDSE ICT Paper 1A multiple-choice questions (2012–2025), published by the Hong Kong Examinations and Assessment Authority (HKEAA), distributed via a [Kaggle dataset](https://www.kaggle.com/datasets/stansir/dataset/data) prepared for this capstone. (same dataset as Capstone #5)
- **Framework:** smolagents `ToolCallingAgent` with managed agents
- **Model:** Claude Haiku 4.5, accessed through LiteLLM
- **Architecture:** 1 Orchestrator + 4 workers (Question Selector, Progress Tracker, Explainer, Question Analyst) and 16 tools
- **Headline design choice:** safeguards are enforced by tool scope (e.g., the answer key is reachable only by the grading tool), not by prompt instructions alone

## Project Structure

```
.
├── agentic_system.ipynb        # Main notebook: agents, tools, example runs, analysis
├── architecture_diagram.png    # System architecture (orchestrator, workers, tools, data)
├── requirements.txt            # Python dependencies
├── env.example                 # Template for API credentials (copy to .env)
└── data/
    ├── final_dataset.csv       # Question bank (one row per question)
    ├── fields_explanation.md   # Description of each column in final_dataset.csv
    ├── ict_curriculum.md       # Module-level curriculum text used for grounded explanations
    └── question/               # Question images referenced by the dataset
```

## Dataset

- **Source:** [DSE ICT Paper 1A on Kaggle](https://www.kaggle.com/datasets/stansir/dataset/data)
- **Size:** 560 questions in the raw file; 519 are usable after excluding the 2025 paper (40 questions) and one withdrawn question
- **Structure:** 26 subcategory tags grouped into 5 modules, plus a `general_knowledge` tag (38 questions); 13 question clusters (26 questions) that share a preamble or figure
- **Difficulty proxy:** `cohort_pct`, the percentage of the HKDSE cohort answering correctly (a descriptive proxy, not a calibrated difficulty estimate)
- **Fields:** see `data/fields_explanation.md`

Place the files exactly as shown in the project structure; the notebook reads `data/final_dataset.csv`, `data/ict_curriculum.md` and the images in `data/question/`.

## Setup

**Requirements:** Python 3.10+ and an API key for the model provider used through LiteLLM. No GPU is needed.

```bash
git clone <this-repository-url>
cd <repository-folder>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env              # then edit .env and add your API key
```

## Usage

Open `agentic_system.ipynb` and run all cells in order. The notebook is organised as:

1. **Setup and data loading:** load the question bank, remove invalid items, build session state
2. **Tools:** 16 deterministic tools (search, serve clusters, grade, mastery, explain, analyse)
3. **Agents:** four worker agents and the orchestrator with its routing rules
4. **Example runs:** 10 scenarios (adaptive practice, answering and feedback, cluster questions, topic analysis, embedded-image question, and others), each followed by an "Observed" note
5. **Evaluation and limitations:** what worked, what failed during development, and what remains

## Key Findings

- Grading, answer hiding, whole-cluster serving and sample-size reporting hold at the **tool level**; they are not guarantees about the exact wording the Tutor writes
- Passing identifiers, not copied text, between agents removed the data drift seen when agents relayed questions in natural language
- Persistent conversation memory (`reset=False`) and a `get_pending_unit` tool were needed so an answer could be matched to the question just served

## Limitations

- Evidence comes from one run of each scenario, in one session, with one model and one question bank; no student used the system, so no learning outcome was measured
- Some Analyst summaries in the final wording still contain errors (for example, a mis-ranked sub-topic in Scenario 7); the underlying tool output is correct
- `cohort_pct` is a proxy for difficulty, and mastery estimates rest on few attempts

## Reproducibility

`requirements.txt` was generated with `pip freeze` and pruned to the packages the notebook needs. LLM outputs can vary between runs even with the same code and data.

## References

See the accompanying report, *Agentic AI System Design Report* (APA 7), for academic citations.

## License

For non-commercial academic study only. Exam questions remain the property of the Hong Kong Examinations and Assessment Authority (HKEAA).
