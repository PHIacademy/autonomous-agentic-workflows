# ICT Paper 1A (Multiple Choice) — Dataset Preparation Pipeline

**Document Version:** 1.5
**Date:** 2026-10-10
**Owner:** Stanley Lo

**Version History:**

| Version | Date | Summary of Changes |
| --- | --- | --- |
| 1.0 | 2026-10-05 | Initial release — 10-column final schema; pipeline design + SOP. |
| 1.1 | 2026-10-05 | Stage 4 schema updated to 13 columns: image fields split into question-body (`qn_image_filename / type / context`) and answer-option (`ans_image_filename / type / context`) families via the literal `_options` suffix dispatch rule. Added answer-option modality detection (`text_only` / `image_only` / `image_plus_text`) and the `[IMAGE]` sentinel for `answer_options` on image-only questions (e.g. 2015 Q18). New duplicate-resolution rules R7–R10. |
| 1.2 | 2026-10-05 | Removed the `ans_image_type` and `ans_image_context` columns. Answer-option images are now described solely by `ans_image_filename`; the final schema is reduced from 13 to 11 columns. Cardinality-parity checks now apply only to the question-body image family. |
| 1.3 | 2026-10-05 | Renamed the `qn_class_id` field to `qn_id` throughout (it encodes subject/paper/year/question only, carrying no classification). |
| 1.4 | 2026-10-05 | Merge-grain redesign. The merge is now driven by the question Markdown (`question/<YYYY>/`) so every question is guaranteed a row. `qn_id` is the sole primary key (one row per question). `qn_type_id` becomes a semicolon-delimited, ascending, de-duplicated list, using the reserved token `unclassified` for untagged questions. `correct_answer` accepts the reserved token `*` for withdrawn questions (e.g. 2018 Q40). New rules R1 (unclassified) and R11 (orphan classification); R1 (duplicate type) is removed. |
| 1.5 | 2026-10-10 | Added cluster-question handling. A "cluster" is a group of questions sharing common context introduced by an "Answer Questions … with reference to …" preamble. The preamble is extracted into new columns `cluster` (0/1 flag) and `cluster_desc` (verbatim shared context), and is stripped from the preceding question's block so it no longer pollutes `answer_options`. Final schema grows from 11 to 13 columns. |

---

## 1. Overview

This document specifies the end-to-end pipeline for constructing the HKDSE ICT Paper 1A (Multiple Choice) structured question dataset. The pipeline ingests three distinct upstream artefacts (a manual classification workbook, answer-key PDFs, and examination question PDFs) and merges them into a single, schema-validated master CSV suitable for downstream model training and Retrieval-Augmented Generation (RAG) workloads.

The pipeline is organised into four linearly dependent stages. Stages 2 and 3 may be executed in parallel with respect to each other; Stage 4 cannot commence until Stages 1–3 have all completed successfully.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                        STAGE 1 — CLASSIFICATION EXTRACTION                     │
│  DSE_MC_Classification.xlsx  ──►  data_preparation.py  ──►  exam_questions.csv │
└───────────────────────────────────┬────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────┐   ┌───────────────────────────┐
│  STAGE 2 — ANSWER OCR   │   │  STAGE 3 — QUESTION OCR   │
│   1A_ans PDFs           │   │  HKDSE Past Paper PDFs    │
│       │                 │   │       │                   │
│       ▼                 │   │       ▼                   │
│ answer/YYYY_1a_ans.md   │   │ question/YYYY/*.{md,      │
│                         │   │       images/, *crops.json│
└────────────┬────────────┘   └──────────────┬────────────┘
             │                               │
             └───────────────┬───────────────┘
                             ▼
          ┌────────────────────────────────────────────────┐
          │   STAGE 4 — DATASET MERGING                    │
          │   exam_questions.csv + answer/*.md             │
          │        + question/YYYY/**                      │
          │                    │                           │
          │                    ▼                           │
          │            final_dataset.csv                   │
          └────────────────────────────────────────────────┘
```

---

## 2. Audience and Usage

This document serves both as:

- **Design Specification (§3–§9):** Architectural contracts, interface schemas, invariants, and field-level validation rules for reviewers and future maintainers.
- **Execution SOP (§10):** Step-by-step commands, prompts, and acceptance checks for operators executing the pipeline.

Readers should cross-reference this document against [fields_explanation.md](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/fields_explanation.md) for the canonical field-level data dictionary (17 fields as of v1.6).

---

## 3. Pre-Requisites and Environment

### 3.1 Software

| Component | Minimum Version | Purpose |
|---|---|---|
| Python | 3.10+ | Execution of `data_preparation.py` and future merge script |
| pandas | Latest stable | Tabular processing inside `data_preparation.py` |
| numpy | Latest stable | Numerical handling of Excel values |
| openpyxl | Latest stable | Excel workbook backend for `pd.read_excel` |
| Trae IDE | Latest stable | Invocation of the `hkdse-ict-pdf-to-md` custom skill |
| OpenCode | Bundled with Trae | PDF-to-Markdown OCR for answer keys |

### 3.2 Upstream Data Locations

All upstream source artefacts reside on the local filesystem (fixed paths per environment):

| Artefact Group | Absolute Path |
|---|---|
| Answer-key PDFs (Paper 1A) | `D:\學校資源\LKC\ICT\HKDSE_ICT_Past_Paper\1A_ans` |
| Examination paper PDFs | `D:\學校資源\LKC\ICT\HKDSE_ICT_Past_Paper` |
| Manual classification workbook | Colocated with `data_preparation.py`; filename: `DSE_MC_Classification.xlsx` (path is CLI-configurable; see §4.2) |

### 3.3 Project Working Directory

All intermediate and final outputs are rooted at:

```
D:\courses\MSc in AI\Capstone\6 Project- Generative AI Applications\generative_ai_applications\data
```

referred to hereinafter as `<DATA_ROOT>`.

---

## 4. Stage 1 — Classification Extraction (data_preparation.py)

Stage 1 transforms the manually-authored multi-sheet classification workbook `DSE_MC_Classification.xlsx` into a machine-readable `exam_questions.csv` that enumerates every question together with its classification identifiers.

### 4.1 Input Contract

| Input | Type | Description |
|---|---|---|
| `DSE_MC_Classification.xlsx` | Excel Workbook (`.xlsx`) | Multi-sheet workbook. Sheet `index` enumerates the taxonomy of `qn_type_id` values. Remaining sheets (`ip`, `cs`, `ia`, `cp`, `si`) each contain a `Year` column and one column per `qn_type_id`. Each cell lists the comma-separated question numbers of that type for the given year. |

### 4.2 Execution

The Python script `data_preparation.py` is located at [data_preparation.py](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/data_preparation.py).

**Command (Windows PowerShell):**

```powershell
cd "<DATA_ROOT>"
.\.venv\Scripts\python.exe data_preparation.py `
    --classification ".\DSE_MC_Classification.xlsx" `
    --output ".\exam_questions.csv"
```

**Command (Git Bash on Windows):**

```bash
cd "<DATA_ROOT>"
./.venv/Scripts/python.exe data_preparation.py \
    --classification "./DSE_MC_Classification.xlsx" \
    --output "./exam_questions.csv"
```

**CLI Arguments:**

| Flag | Short | Required | Default | Description |
|---|---|---|---|---|
| `--classification` | `-c` | No | `DSE_MC_Classification.xlsx` (falls back to `/data/DSE_MC_Classification.xlsx` if mount exists) | Path to the classification workbook |
| `--output` | `-o` | No | `exam_questions.csv` (falls back to `/data/exam_questions.csv`) | Path to the output CSV |

### 4.3 Processing Logic

1. **Index sheet load.** The `index` sheet is read and parsed into an in-memory `qn_type_mapping` of `{qn_type_id → {core_type, cat_id, cat_name, subcat_id, subcat_name}}`.
2. **Core sheets load.** Every remaining sheet (one per category code: `ip`, `cs`, `ia`, `cp`, `si`) is loaded into `self.core_sheets`.
3. **Question-number parsing.** For every non-`Year` column, each cell value is split on commas and coerced to integers. Values `1`, `"1"`, `"1.0"`, and `"1, 2, 3"` are all handled.
4. **`qn_id` formatting.** For each `(year, question_number)` tuple discovered, `qn_id` is synthesised per [fields_explanation.md §2.7](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/fields_explanation.md) using the template `ICT_{year}_paper1a_q{NN:02d}`.
5. **Validation.** Built-in checks flag missing columns, null values, malformed `qn_id` strings, and years outside `2012–2024`. Warnings do not abort the pipeline.
6. **Output save.** Rows are sorted deterministically by `(paper_year, qn_type_id, qn_id)` and written to CSV with no row index.

### 4.4 Output Contract — `exam_questions.csv`

| Column | Type | Format / Constraints |
|---|---|---|
| `qn_type_id` | string | `<core_type>_<cat_id>_<subcat_id>` (lowercase, underscore delimited) |
| `qn_id` | string | `ICT_{YYYY}_paper1a_q{NN:02d}` — not necessarily unique in this intermediate file |
| `paper_year` | integer | `2012 ≤ paper_year ≤ 2024` |

- **Cardinality:** One row per (question, qn_type_id) pair. The same `qn_id` may appear multiple times if a question is tagged under multiple types. This is an **intermediate** lookup file; `final_dataset.csv` (Stage 4) collapses these into one row per `qn_id`.
- **Primary key (logical):** `(qn_id, qn_type_id)` — this key applies only to `exam_questions.csv`, not to the final dataset.

### 4.5 Acceptance Criteria

- [ ] Script exits with code `0`.
- [ ] Log line `✅ EXTRACTION COMPLETE` is emitted.
- [ ] Validation log contains `✅ Validation PASSED` (or documented warnings have been reviewed and accepted).
- [ ] Output CSV contains the three columns listed in §4.4 in the order specified.
- [ ] All `qn_id` values match the regex `^ICT_\d{4}_paper1a_q\d{2}$`.

---

## 5. Stage 2 — Answer-Key OCR

Stage 2 converts each answer-key PDF into a Markdown file via OCR. Outputs for all years are written to `<DATA_ROOT>\answer`.

### 5.1 Input Contract

| Input | Type | Description |
|---|---|---|
| `<UPSTREAM>\1A_ans\*.pdf` | PDF documents | One or more answer-key PDFs, one per examination year. |

### 5.2 Execution

This stage is executed inside the Trae IDE using the bundled **OpenCode** OCR engine.

**OpenCode Prompt (issued from within the `1A_ans` upstream directory):**

> Inside folder 1A_ans, OCR all pdf and convert to md files. After conversion, help me verify the texts in md is 100% match those text on the pdf.

### 5.3 Output Contract

All outputs are deposited in `<DATA_ROOT>\answer`.

| Output | Naming Convention | Structure |
|---|---|---|
| Answer key Markdown | `<YYYY>_1a_ans.md` — four-digit year, underscore, literal `1a_ans`, extension `.md` | H2 header line `Paper 1 (Section A)` followed by a Markdown table with columns `\| Question No. \| Key \| Question No. \| Key \|`. Each cell in the `Key` column takes one of two forms: `<LETTER> (<NN>%)` (a letter `A–Z` and an optional cohort percentage in round parentheses), or the literal `*` for a withdrawn/cancelled question. |

**Concrete example (2021):** [answer/2021_1a_ans.md](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/answer/2021_1a_ans.md)

### 5.4 Acceptance Criteria

- [ ] Every year present in the upstream directory has a matching `<YYYY>_1a_ans.md` file.
- [ ] Each Markdown file contains exactly 40 valid answer keys (Paper 1A has 40 MC questions); a withdrawn question is recorded as `*`.
- [ ] The operator has manually reviewed every file in a side-by-side visual comparison against the source PDF, per the "100% match" OCR prompt, and any transcription errors have been manually corrected before progressing.

---

## 6. Stage 3 — Examination Question OCR

Stage 3 converts the full examination paper PDFs into Markdown, cropped image assets, and a machine-readable JSON crop manifest for each year.

### 6.1 Input Contract

| Input | Type | Description |
|---|---|---|
| `<UPSTREAM>\*.pdf` | PDF documents | Full HKDSE ICT Paper 1A question papers, one per year. |

### 6.2 Execution

This stage is executed inside the **Trae IDE** via the registered custom skill `/hkdse-ict-pdf-to-md`, invoked once per year. The skill encapsulates both the PDF→Markdown OCR pass and the element cropping step.

### 6.3 Output Contract

Outputs for each year are deposited in `<DATA_ROOT>\question\<YYYY>\`, with the following per-year structure (illustrative example for year 2021 shown at [question/2021/](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/question/2021)):

| Output | Relative Path Pattern | Description |
|---|---|---|
| Question Markdown | `<YYYY>DSE1A.md` | Full OCR transcript of the paper. Each question starts with a bolded numbered header `**<NN>.** …`. Options are encoded as lines `A. …`, `B. …`, `C. …`, `D. …`, or as a Markdown table whose first column holds the letter. Embedded figures are included via Markdown image links `![alt](<relative_image_path>)`. |
| Cropped image directory | `<YYYY>DSE1A_images\` | Directory containing all graphical assets (PNG format) extracted from the paper. |
| Individual image asset | `<YYYY>DSE1A_images\<YYYY>DSE1A_q<NN>_<context>.png` | One PNG per cropped element. The filename suffix `<context>` is the free-form semantic label also recorded in `qn_image_context`. |
| Crop manifest JSON | `<YYYY>DSE1A_crops.json` | Machine-readable manifest enumerating every cropped element and its detection metadata. Schema detailed in §6.4. |

### 6.4 Crop Manifest JSON Schema — `<YYYY>DSE1A_crops.json`

Concrete example: [2021DSE1A_crops.json](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/question/2021/2021DSE1A_crops.json)

| Top-Level Key | Type | Description |
|---|---|---|
| `buf` | number | Layout buffer constant (in points) used during cropping. |
| `question_count` | integer | Number of questions in the paper (expected value: `40` for Paper 1A). |
| `skip_pages` | integer | Number of leading pages skipped during page-aware cropping. |
| `elements` | `Element[]` | Array of zero or more cropped-element records, ordered by ascending `page` then by document order. |

**Element record schema:**

| Key | Type | Required | Values / Format |
|---|---|---|---|
| `page` | integer | Yes | 1-indexed PDF page number on which the element was detected. |
| `name` | string | Yes | Token `"<YYYY>DSE1A_q<NN>_<context>"`. The suffix after the final underscore defines the `qn_image_context` for this element. Dispatch rule for v1.1+ (see §7.2 step 5): names ending in the literal suffix `_options` are routed to `ans_image_filename`; all others are routed to the `qn_image_*` family. |
| `win` | `[number, number, number, number]` | Yes | Bounding box `[x1, y1, x2, y2]` in PDF point space. |
| `kind` | string | No (defaults to `"figure"` when absent) | One of `"table"` \| `"figure"` \| `"code"` — see [fields_explanation.md §2.10](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/fields_explanation.md). |
| `allow` | `string[]` | No | Set of detection-tolerance flags emitted during cropping (e.g., `"FALLBACK"`, `"CLAMPED"`, `"EDGE_INK"`, `"INK_OUTSIDE_BORDER"`). Informational only — not forwarded to the final CSV. |

### 6.5 Acceptance Criteria

- [ ] Each year in scope produces exactly one `<YYYY>DSE1A.md`, one `<YYYY>DSE1A_crops.json`, and a matching `<YYYY>DSE1A_images\` directory.
- [ ] Every image referenced via Markdown `![](…)` syntax in `<YYYY>DSE1A.md` exists on disk under the images directory.
- [ ] Every entry in the `elements` array of the crop manifest has a corresponding PNG file under the images directory (name consistent with `<name>.png`).
- [ ] Questions are numbered contiguously `1..40` within the Markdown file.

---

## 7. Stage 4 — Dataset Merging (Logic Specification; Script TBD)

Stage 4 unifies the three upstream outputs of Stages 1, 2, and 3 into a single master dataset. This section defines the **functional specification** of Stage 4; a concrete implementation (e.g., `merge_dataset.py`) is deferred and must reproduce the behaviour described below.

### 7.1 Inputs to the Merge

| ID | Source Stage | Artefact | Key(s) |
|---|---|---|---|
| (A) | Stage 1 | `<DATA_ROOT>\exam_questions.csv` | `qn_id`, `qn_type_id`, `paper_year` |
| (B) | Stage 2 | `<DATA_ROOT>\answer\<YYYY>_1a_ans.md` | Year `YYYY`, per-row question number |
| (C) | Stage 3 | `<DATA_ROOT>\question\<YYYY>\<YYYY>DSE1A.md` | Year `YYYY`, per-block question number |
| (D) | Stage 3 | `<DATA_ROOT>\question\<YYYY>\<YYYY>DSE1A_crops.json` | Year `YYYY`, per-element `name` (contains question number) |

### 7.2 Join Strategy (Logical)

The merge produces **exactly one row per question**. The universe of questions is driven by the per-year question Markdown (C), not by `exam_questions.csv` (A), so that every question is guaranteed a row even when it is not classified.

1. **Universe enumeration.** Enumerate examination years from the `question/<YYYY>/` directory. For each year, read `<YYYY>DSE1A.md` and split it on line-starting question anchors `**<NN>.**` (§6.3). Each resulting question block produces exactly one output row keyed by its `qn_id`.

2. **Cluster detection and stripping.** Scan the raw Markdown for cluster preambles — lines matching `Answer Questions <nums> with reference to …`. Each preamble, together with everything up to (but excluding) the next question anchor, is the shared context:
   - Assign a unique cluster id and record every question number named in the preamble as a member of that cluster.
   - Capture the preamble text verbatim (intro sentence + any image links + any fenced code) as the cluster's `cluster_desc`.
   - **Strip** the preamble span from the raw text before splitting, so it no longer lands in the preceding question's `answer_options`.
   - In the output, each cluster member gets `cluster = 1` and the identical `cluster_desc`; all other questions get `cluster = 0` and an empty `cluster_desc`.

3. **Question body (C).** For each question block:
   - `qn_id` → synthesise as `ICT_{YYYY}_paper1a_q{NN:02d}`.
   - `paper_year` → the year `YYYY`.
   - `question_text` → the concatenated body of the question up to (but not including) the next question, preserving original Markdown.
   - **Answer-option modality detection** → inspect the trailing region of the question body for (i) textual option markers `A.`/`B.`/`C.`/`D.` and (ii) Markdown image links whose target matches an option-suffixed element from (D). The combination determines the modality and the value emitted to `answer_options`:
     - **text_only** — textual markers present and no option image ⇒ emit the contiguous `A.`/`B.`/`C.`/`D.` block verbatim.
     - **image_only** — no textual markers but an option image present ⇒ emit the literal sentinel `[IMAGE]`.
     - **image_plus_text** — both present ⇒ emit the contiguous `A.`/`B.`/`C.`/`D.` block verbatim; the option image is captured by `ans_image_filename` in step 5.

4. **Classification data (A).** For each `qn_id`, look up all rows in `exam_questions.csv`:
   - Collect every associated `qn_type_id`, sort ascending lexicographically, de-duplicate, and join with `;` into a single `qn_type_id` list.
   - If **no** rows match (the question is not classified), set `qn_type_id = "unclassified"` and surface a warning (Rule R1).

5. **Answer key (B).** Parse `<YYYY>_1a_ans.md`, match question numbers from the two-column table:
   - `correct_answer` → uppercase letter `A–Z`, extracted as the first token of the `Key` cell. If the cell is the literal `*`, record `correct_answer = "*"` (withdrawn question).
   - `cohort_pct` → optional integer captured from inside the trailing parentheses, if present (e.g., cell `"C (51%)"` yields `correct_answer = "C"`, `cohort_pct = 51`). If the cohort percentage is absent, `cohort_pct` is set to an empty value (not `0`).

6. **Image metadata (D).** From `<YYYY>DSE1A_crops.json`, partition elements whose `name` matches `<YYYY>DSE1A_q<NN>_*` into two disjoint groups by name suffix:
   - **Question-body group** — every element whose `name` does **not** end in `_options` (nor is listed as an option-image override). Cardinality across `qn_image_filename`, `qn_image_type`, and `qn_image_context` **must** be identical:
     - `qn_image_filename` → semicolon-delimited list of `"question/<YYYY>/<YYYY>DSE1A_images/<name>.png"`, in manifest order.
     - `qn_image_type` → semicolon-delimited list of each element's `kind` (default `"figure"` if absent).
     - `qn_image_context` → semicolon-delimited list of the `<context>` suffix.
   - **Answer-option group** — every element whose `name` ends in `_options` (or is a listed option-image override). Only the filename is retained:
     - `ans_image_filename` → semicolon-delimited list of relative paths, formatted identically to the question-body group.
   - **Empty cases.** A question with no elements leaves all four image fields empty (`""`/NA). The question-body and answer-option families are independent.

7. **Orphan classifications.** Any `qn_id` present in `exam_questions.csv` but absent from the question Markdown is surfaced as a warning (Rule R11) and is **not** emitted (it has no question content).

### 7.3 Cardinality and Duplicate Resolution Rules

| Rule ID | Scenario | Disposition |
|---|---|---|
| R1 | No `qn_type_id` rows for a `qn_id` in `exam_questions.csv` (unclassified question) | Surface a warning; set `qn_type_id = "unclassified"`. The row is still written. |
| R2 | Question body split fails (missing `**NN.` anchor) | Surface a warning; skip the question and require manual triage. |
| R3 | Answer key table has fewer than 40 entries for a year | Surface a warning; block the merge until manually corrected in the Stage 2 output. |
| R4 | `elements[i].kind` absent | Apply default value `"figure"` per [fields_explanation.md §2.10](file:///d:/courses/MSc%20in%20AI/Capstone/6%20Project-%20Generative%20AI%20Applications/generative_ai_applications/data/fields_explanation.md) — do **not** treat as missing data. |
| R5 | Image referenced in Markdown but absent from `elements` array | Surface a warning; the image path is **not** promoted into `qn_image_filename` or `ans_image_filename` — the crop manifest is the canonical source of truth. |
| R6 | Unequal list lengths within the question-body family (`qn_image_filename` / `qn_image_type` / `qn_image_context`) | Surface a warning; block the row from output until manual reconciliation. |
| R7 | Cross-family anomaly: an element appears to belong to both (e.g., element `name` ends in `_options` but was also referenced in the question body) | Surface a warning; the dispatch rule (§7.2 step 5) takes precedence and the element is routed to the answer-option family. |
| R8 | `answer_options` modality inconsistency — `answer_options == "[IMAGE]"` **and** `ans_image_filename` is empty | Surface a warning; the row is **data loss** — no textual options and no image to convey the choices. Block the row and require manual triage (re-OCR the source PDF for that question). |
| R9 | `answer_options` is **empty** (not the `[IMAGE]` token) | Surface a warning; block the row. Empty `answer_options` is invalid under every modality — even image-only questions must carry the literal sentinel `[IMAGE]`. |
| R10 | `answer_options` is textual (begins with `A.`) **and** `ans_image_filename` is non-empty | Admissible modality `image_plus_text`. No warning. The textual options are preserved verbatim; the auxiliary image is recorded in `ans_image_filename`. |
| R11 | A `qn_id` exists in `exam_questions.csv` but not in the question Markdown (orphan classification) | Surface a warning; do not emit a row (there is no question content to attach). |

### 7.4 Output Contract — `final_dataset.csv`

Output path: `<DATA_ROOT>\final_dataset.csv`

**Final schema — 13 columns, one row per question, sorted by `(paper_year, qn_id)`:**

| # | Column | Type | Cardinality / Format |
|---|---|---|---|
| 1 | `qn_type_id` | string | Semicolon-delimited, ascending, de-duplicated list of `qn_type_id` values; or the reserved token `unclassified` |
| 2 | `qn_id` | string | `ICT_{YYYY}_paper1a_q{NN:02d}` — globally unique (primary key) |
| 3 | `paper_year` | integer | `2012 ≤ paper_year ≤ 2024` |
| 4 | `question_text` | string | Markdown string; may embed `![](…)` image links |
| 5 | `qn_image_filename` | string | Semicolon-delimited relative paths; empty for questions with no question-body image |
| 6 | `qn_image_type` | string | Semicolon-delimited list of `kind` tokens; empty for questions with no question-body image |
| 7 | `qn_image_context` | string | Semicolon-delimited list of `<context>` tokens; empty for questions with no question-body image |
| 8 | `ans_image_filename` | string | Semicolon-delimited relative paths for option-block images; empty when answer options are purely textual (modality `text_only`) |
| 9 | `answer_options` | string | Markdown block containing `A. …` through `D. …` (modality `text_only` or `image_plus_text`), OR the literal sentinel `[IMAGE]` (modality `image_only`) |
| 10 | `correct_answer` | string | Single uppercase letter `A–Z`, or the literal `*` for a withdrawn question |
| 11 | `cohort_pct` | integer, nullable | Cohort percentage (0–100); empty if the percentage was not printed in the answer key |
| 12 | `cluster` | integer | `1` if the question belongs to a cluster (shares context with siblings), else `0` |
| 13 | `cluster_desc` | string | Verbatim shared context for cluster members; empty when `cluster = 0` |

### 7.5 Acceptance Criteria

- [ ] Final CSV contains exactly 13 columns in the order specified in §7.4.
- [ ] Row count equals `|years| × 40` exactly (one row per question); for the current dataset `14 × 40 = 560`.
- [ ] All `qn_id` values match the regex `^ICT_\d{4}_paper1a_q\d{2}$`.
- [ ] `qn_id` values are globally unique (no duplicate rows).
- [ ] Every `qn_type_id` value is non-empty — either the token `unclassified` or a non-empty, ascending, de-duplicated `;`-joined list.
- [ ] Every `correct_answer` value is a single uppercase letter `A–Z` **or** the literal `*`.
- [ ] Every `answer_options` value is **either** the literal sentinel `[IMAGE]` **or** a non-empty Markdown block whose first non-whitespace token begins with `A.` (or `|` for table-format options).
- [ ] For every row where `answer_options == "[IMAGE]"`, the `ans_image_filename` column must be non-empty.
- [ ] For every row, the cardinalities of the semicolon-split lists **within** the question-body family (`qn_image_filename`, `qn_image_type`, `qn_image_context`) are equal.
- [ ] Every `cluster` value is `0` or `1`.
- [ ] For every row where `cluster == 1`, `cluster_desc` is non-empty; and all members of a cluster carry identical `cluster_desc`.
- [ ] For every row where `cluster == 0`, `cluster_desc` is empty.
- [ ] Zero unresolved `Rule R2/R3/R6/R8/R9` warnings (blocking); `R1/R5/R7/R11` warnings are surfaced but non-blocking.

---

## 8. Invariants and Cross-Cutting Validation Rules

| ID | Invariant | Enforced At |
|---|---|---|
| I1 | `qn_id` is the sole primary key across the final dataset (exactly one row per question). | Stage 4 output validator |
| I2 | `paper_year` and the year segment embedded in `qn_id` are equal. | Stage 4 output validator |
| I3 | For each question row, `correct_answer ∈ {A, B, C, D} ∪ {"*"}` (the answer key never exceeds four MC options, except the withdrawn marker `*`). | Stage 4 output validator |
| I4 | `qn_image_type` entries are drawn exclusively from `{table, figure, code}`. | Stage 4 output validator (rejects unknown `kind` values) |
| I5 | Paths in `qn_image_filename` and `ans_image_filename` (once split and resolved against `<DATA_ROOT>`) exist on disk. | Stage 4 output validator (optional filesystem pass) |
| I6 | `answer_options` is **never** empty. It carries either textual `A. … D. …` or the literal sentinel `[IMAGE]`. | Stage 4 output validator (Rule R9) |
| I7 | `cluster ∈ {0, 1}`. When `cluster = 1`, `cluster_desc` is non-empty; when `cluster = 0`, `cluster_desc` is empty. | Stage 4 output validator |

---

## 9. Failure and Escalation Paths

| Event | Immediate Action | Escalation |
|---|---|---|
| Stage 1 exits non-zero or emits `❌ Validation FAILED` | Inspect `stderr` for root cause; re-run with a fresh copy of the classification workbook. | If workbook structural corruption is suspected, notify the workbook author. |
| Stage 2 or 3 OCR produces visual misalignment / truncated text | Manually edit the relevant `.md` file in place and re-verify side-by-side. | For systemic OCR failures across a year, re-run the OCR skill with different parameters. |
| Stage 4 merge log contains R2/R3/R6/R8/R9 warnings | Open triage ticket per warning; resolve in the corresponding upstream artefact and re-run the merge. | If volume of warnings exceeds 5 % of rows, abort the merge and conduct end-to-end re-review of Stages 2–3 for that year. |

---

## 10. Standard Operating Procedure (Operator Checklist)

The following numbered checklist must be executed in order, with each `[x]` item signed off by the operator before proceeding to the next stage.

### 10.1 Pre-Flight

- [ ] Confirm all pre-requisites in §3.1 are installed in the local environment.
- [ ] Confirm all three upstream paths in §3.2 are readable.
- [ ] Confirm `<DATA_ROOT>` is writable and has ≥ 500 MB of free disk space (images for ~10 years typically occupy ~300 MB).

### 10.2 Execute Stage 1

- [ ] Run the command in §4.2.
- [ ] Verify Stage 1 acceptance criteria in §4.5.
- [ ] Archive the terminal log to `logs\stage1_YYYYMMDD.log`.

### 10.3 Execute Stages 2 and 3 (Parallel)

- [ ] Invoke OpenCode prompt in §5.2 against the `1A_ans` upstream directory.
- [ ] Move the generated Markdown files to `<DATA_ROOT>\answer` and rename each to `<YYYY>_1a_ans.md` per §5.3.
- [ ] Verify Stage 2 acceptance criteria in §5.4.
- [ ] Independently invoke `/hkdse-ict-pdf-to-md` once per examination year.
- [ ] Deposit outputs under `<DATA_ROOT>\question\<YYYY>\` per §6.3.
- [ ] Verify Stage 3 acceptance criteria in §6.5.
- [ ] Archive terminal / skill logs to `logs\stage2_YYYYMMDD.log` and `logs\stage3_YYYYMMDD.log`.

### 10.4 Execute Stage 4

- [ ] Implement or obtain the Stage 4 merge script conforming to §7 (functional specification).
- [ ] Execute the merge script against `<DATA_ROOT>`.
- [ ] Review the merge log report for any warnings. Resolve per §7.3 / §9.
- [ ] Verify Stage 4 acceptance criteria in §7.5.
- [ ] Confirm `<DATA_ROOT>\final_dataset.csv` exists on disk and conforms to §7.4.
- [ ] Archive the merge log report to `logs\stage4_YYYYMMDD.log`.

### 10.5 Post-Completion

- [ ] Produce a single SHA-256 checksum file for `final_dataset.csv` (e.g., via `Get-FileHash -Algorithm SHA256` in PowerShell) and store it alongside the CSV for reproducibility.
- [ ] Confirm downstream consumers (RAG embedding pipeline, LLM training notebook) can read `final_dataset.csv` without schema errors.
