"""
Stage 4 Dataset Merge Script

Purpose:
    Merges the outputs of Stages 1-3 (exam_questions.csv, answer/*.md,
    question/<YYYY>/<YYYY>DSE1A.md, question/<YYYY>/<YYYY>DSE1A_crops.json) into a
    single, schema-validated final_dataset.csv conforming to fields_explanation.md
    v1.5 and final_capstone_flow.md v1.4.

    The merge is driven by the per-year question Markdown (question/<YYYY>/), so
    every question is guaranteed exactly one output row. Classifications from
    exam_questions.csv are left-joined and aggregated into a single list-valued
    `qn_type_id` column (reserved token `unclassified` when a question has no
    classification). The answer key's literal `*` is preserved for withdrawn
    questions.

Inputs:
    - exam_questions.csv: Classification with (qn_type_id, qn_id, paper_year)
    - answer/<YYYY>_1a_ans.md: Answer keys, one file per year (2012-2025)
    - question/<YYYY>/<YYYY>DSE1A.md: OCR'd question text per year
    - question/<YYYY>/<YYYY>DSE1A_crops.json: Canonical element manifest per year

Output:
    - final_dataset.csv: 11-column merged dataset (one row per qn_id)
    - validation_report.json: Summary of rule violations and warnings

Usage:
    python merge_dataset.py --data-root ./ --output ./final_dataset.csv

Author: Stanley Lo
Date: 2026-10-05
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Schema-level constants ---------------------------------------------------
SUBJECT = 'ICT'                # Project-wide subject code prefix in qn_id
PAPER = 'paper1a'              # Paper segment used when synthesising qn_id
QUESTIONS_PER_PAPER = 40       # Paper 1A always contains 40 multiple-choice questions
IMAGE_ONLY_SENTINEL = '[IMAGE]'  # Literal token for answer_options when options are image-only
DEFAULT_IMAGE_KIND = 'figure'    # Fallback per §2.10 when crops.json element has no `kind` key
OPTIONS_SUFFIX = '_options'      # Name suffix that routes an element to ans_image_filename
UNCLASSIFIED_TOKEN = 'unclassified'  # Reserved qn_type_id value for an untagged question
WITHDRAWN_ANSWER = '*'           # Reserved correct_answer value for a withdrawn question

# Manual overrides for image-only answer options whose crop-element name does
# NOT end with the conventional `_options` suffix. Without these, such images
# would be mis-routed to the question-body family and the question would be
# blocked as R9 (no textual options, no options image).
#
# Example: 2017 Q13 stores its A/B/C/D chart options inside
# `2017DSE1A_q13_charts.png` (not `..._q13_options.png`), so it is listed here
# to ensure the options link is preserved in `ans_image_filename`.
OPTIONS_IMAGE_OVERRIDES = {
    '2017DSE1A_q13_charts',
}

# Final CSV columns in canonical 11-column order (final_capstone_flow.md v1.4 §7.4)
OUTPUT_COLUMNS = [
    'qn_type_id',          # 1  — semicolon-delimited list of types, or 'unclassified'
    'qn_id',               # 2  — global question identifier (ICT_YYYY_paper1a_qNN); primary key
    'paper_year',          # 3  — examination year (e.g. 2021)
    'question_text',       # 4  — Markdown question stem (up to but not including A./B./C./D.)
    'qn_image_filename',   # 5  — question-body image paths (semicolon list; empty if none)
    'qn_image_type',       # 6  — question-body kinds (table / figure / code; semicolon list)
    'qn_image_context',    # 7  — question-body context tokens (suffix from element name)
    'ans_image_filename',  # 8  — answer-option image paths (semicolon list; empty if none)
    'answer_options',      # 9  — A…D text block or sentinel [IMAGE]
    'correct_answer',      # 10 — single uppercase letter A–Z, or '*' for withdrawn
    'cohort_pct',          # 11 — cohort % from answer key; empty if not published
    'cluster',             # 12 — 1 if the question shares context with another, else 0
    'cluster_desc',        # 13 — shared context for cluster members; empty otherwise
]

# --- Regular expressions ------------------------------------------------------
# qn_id:  ICT_<YYYY>_paper1a_q<NN>  (captures year & question number)
QN_ID_REGEX = re.compile(r'^ICT_(\d{4})_paper1a_q(\d{2})$')

# Markdown question anchor at start of a line:  **<NN>.** <text>
# Note: the closing `**` follows immediately after the period, then whitespace,
# then the question body. e.g.  "**1.** An 8-bit code..."
# Anchors must be at the line start (^\*\*) so mid-question bold headings
# (e.g., a bolded passage reference) are not misinterpreted as new questions.
QUESTION_ANCHOR_REGEX = re.compile(r'^\*\*(\d{1,2})\.\*\*\s', re.MULTILINE)

# Textual answer-option markers. Papers use TWO layout styles:
#   Line format:  "A. 00111111"            (letter at start of line, then text)
#   Line format:  "A."                     (letter alone at end of line; the option
#                                           body follows on the next line — used when
#                                           options are code snippets, e.g. 2015 Q25)
#   Table format: "| A. | 11101001 | ..."  (letter in first cell of a Markdown table)
# The `(?:\s|$)` alternative is essential: some option letters are immediately
# followed by end-of-line rather than a space. This pattern is specific enough
# that algorithm pseudocode (``A <- 1``, ``A:``, ``SELECT ...``) never matches.
LINE_OPTION_REGEX = re.compile(r'^([A-D])\.(?:\s|$)', re.MULTILINE)
TABLE_OPTION_ROW_REGEX = re.compile(r'^\|\s*([A-D])\.?\s*\|', re.MULTILINE)
# Any Markdown table row (used to walk back and capture the header + separator
# rows that precede a table-formatted options block).
TABLE_ROW_REGEX = re.compile(r'^\s*\|', re.MULTILINE)

# Answer-key Markdown table cell:
#   | <NN>. | <LETTER> [(<NN>[%])] |   OR   | <NN>. | * |
# Each physical line carries TWO cells (questions 1-20 and 21-40 are laid out
# side-by-side). We scan cells independently with finditer. The trailing `%` is
# optional (2015 prints `(55)`, 2021 prints `(40%)`). The literal `*` marks a
# withdrawn/cancelled question.
ANSWER_KEY_CELL_REGEX = re.compile(
    r'\|\s*(\d+)\.\s*\|\s*([A-Z](?:\s*\(\d+%?\))?|\*)\s*(?=\|)'
)

# Cluster preamble: a line introducing multiple questions that share common
# context, e.g. "Answer Questions 28 and 29 with reference to the following
# algorithms.". The number list is captured (supports both "X and Y" and the
# more general "X, Y and Z"), and the full preamble (intro sentence + any image
# links + any fenced code) is treated as the shared context.
CLUSTER_INTRO_REGEX = re.compile(
    r'^Answer Questions\s+(.*?)\s+with reference to',
    re.MULTILINE
)


class MergeReport:
    """Accumulates non-blocking warnings and blocking rule violations for later review.

    Two severity buckets are used, matching the rule-system defined in
    final_capstone_flow.md §7.3:

    * ``warnings`` — issues surfaced for manual review but that do NOT abort the
      pipeline (e.g. R1 unclassified, R6 cardinality-mismatch, R11 orphan).
    * ``blocked_rows`` — issues that MUST be resolved before the CSV is considered
      valid (R8 / R9 / malformed qn_id / malformed correct_answer).
    """

    def __init__(self):
        self.warnings: List[Dict] = []
        self.blocked_rows: List[Dict] = []

    def warn(self, rule_id: str, qn_id: str, message: str):
        """Record a non-blocking warning (surfaced in validation_report.json)."""
        self.warnings.append({
            'rule': rule_id,
            'qn_id': qn_id,
            'message': message,
        })

    def block(self, rule_id: str, qn_id: str, message: str):
        """Record a blocking violation — the affected row is NOT written to CSV."""
        self.blocked_rows.append({
            'rule': rule_id,
            'qn_id': qn_id,
            'message': message,
        })

    def to_dict(self) -> Dict:
        """Convert accumulated findings into a JSON-serialisable dict."""
        return {
            'warnings': self.warnings,
            'blocked_rows': self.blocked_rows,
            'warning_count': len(self.warnings),
            'blocked_count': len(self.blocked_rows),
        }


class DatasetMerger:
    """Orchestrates the Stage 4 merge pipeline.

    Responsibilities:
      * Enumerate examination years from the ``question/<YYYY>/`` directory.
      * Load the classification CSV, answer-key Markdown, question Markdown,
        and crop manifest JSON for every year.
      * Produce exactly one output row per question (keyed by ``qn_id``),
        driving the iteration from the question Markdown (§7.2).
      * Aggregate each question's ``qn_type_id`` values into an ascending,
        de-duplicated, semicolon-delimited list (or the ``unclassified`` token).
      * Partition per-question crop elements into the question-body and
        answer-option families via the ``_options`` suffix (§7.2 step 5).
      * Classify each question's answer-option block into one of the three
        modalities (text_only / image_only / image_plus_text).
      * Validate every row against the schema invariants in §8 (strict mode).
      * Write final_dataset.csv only if zero blocking violations are present.
    """

    def __init__(self, data_root: str):
        self.data_root = Path(data_root)
        self.classification_df: Optional[pd.DataFrame] = None
        # answer_keys[year][qnum] -> (letter_or_asterisk, cohort_pct or None)
        self.answer_keys: Dict[int, Dict[int, Tuple[str, Optional[int]]]] = {}
        # questions[year][qnum] -> {question_text, answer_options_raw, has_textual_options}
        self.questions: Dict[int, Dict[int, Dict]] = {}
        # crop_elements[year][qnum] -> List[element dict] (from crops.json)
        self.crop_elements: Dict[int, Dict[int, List[Dict]]] = {}
        # cluster_by_qnum[year][qnum] -> cluster_id (only cluster members)
        self.cluster_by_qnum: Dict[int, Dict[int, int]] = {}
        # cluster_desc[year][cluster_id] -> shared-context preamble text
        self.cluster_desc: Dict[int, Dict[int, str]] = {}
        self.report = MergeReport()

    def run(self, output_path: str, validation_report_path: str) -> bool:
        """Execute the full load → merge → validate → write pipeline.

        Returns
        -------
        bool
            ``True`` if the pipeline completed successfully and an output CSV
            was written to ``output_path``; ``False`` otherwise.
        """
        logger.info('=' * 70)
        logger.info('STAGE 4 DATASET MERGE PIPELINE')
        logger.info('Schema per fields_explanation.md v1.5 / final_capstone_flow.md v1.4')
        logger.info('=' * 70)

        if not self._load_classification():
            return False

        for year in self._enumerate_years():
            if not self._load_year(year):
                logger.error(f'Failed to load year {year}')
                return False

        records = self._merge_all()

        validation_ok = self._validate(records)
        if not validation_ok:
            logger.error('Validation produced blocking errors; aborting write')
            return False

        self._write_output(records, output_path)
        self._write_report(validation_report_path)

        logger.info('=' * 70)
        logger.info('MERGE COMPLETE')
        logger.info(f'Total rows: {len(records)}')
        logger.info(f'Warnings: {len(self.report.warnings)}')
        logger.info(f'Blocked rows: {len(self.report.blocked_rows)}')
        logger.info('=' * 70)
        return True

    def _enumerate_years(self) -> List[int]:
        """Return the sorted examination years present under ``question/<YYYY>/``.

        The question Markdown is the authoritative universe of questions (§7.2
        step 1), so the set of years is derived from the question directory
        rather than from ``exam_questions.csv``.
        """
        qdir = self.data_root / 'question'
        years: List[int] = []
        if qdir.exists():
            for p in qdir.iterdir():
                if p.is_dir() and re.fullmatch(r'\d{4}', p.name):
                    years.append(int(p.name))
        return sorted(years)

    def _load_classification(self) -> bool:
        """Read exam_questions.csv produced by Stage 1.

        Returns ``False`` if the file is missing or unparseable, ``True``
        otherwise.
        """
        path = self.data_root / 'exam_questions.csv'
        if not path.exists():
            logger.error(f'Classification file not found: {path}')
            return False
        try:
            self.classification_df = pd.read_csv(path)
            logger.info(f'Loaded classification: {len(self.classification_df)} rows')
            return True
        except Exception as e:
            logger.error(f'Failed to load classification: {e}')
            return False

    def _load_year(self, year: int) -> bool:
        """Load all three per-year upstream artefacts (answer key, markdown,
        crops manifest) for the given examination year.

        Returns ``False`` if any of the three files is missing or unparseable.
        """
        answer_path = self.data_root / 'answer' / f'{year}_1a_ans.md'
        md_path = self.data_root / 'question' / str(year) / f'{year}DSE1A.md'
        crops_path = self.data_root / 'question' / str(year) / f'{year}DSE1A_crops.json'

        if not answer_path.exists():
            logger.error(f'Missing answer file: {answer_path}')
            return False
        if not md_path.exists():
            logger.error(f'Missing question md: {md_path}')
            return False
        if not crops_path.exists():
            logger.error(f'Missing crops json: {crops_path}')
            return False

        try:
            self.answer_keys[year] = self._parse_answer_key(answer_path)
            self.questions[year] = self._parse_questions(md_path, year)
            self.crop_elements[year] = self._parse_crops(crops_path)
            logger.info(f'Loaded year {year}: {len(self.answer_keys[year])} answers, '
                        f'{len(self.questions[year])} questions, '
                        f'{sum(len(v) for v in self.crop_elements[year].values())} elements')
            return True
        except Exception as e:
            logger.error(f'Failed to load year {year}: {e}')
            return False

    def _parse_answer_key(self, path: Path) -> Dict[int, Tuple[str, Optional[int]]]:
        """Parse an `<YYYY>_1a_ans.md` Markdown table into a dict keyed by qnum.

        Returns mapping ``{qnum: (letter_or_asterisk, cohort_pct | None)}``.
        The cohort percentage is optional; when absent the second tuple element
        is ``None``. A literal ``*`` cell is preserved as-is (withdrawn question).
        Because each physical table row holds two (Question No., Key) pairs, we
        iterate every matched cell independently via ``finditer``.
        """
        result = {}
        text = path.read_text(encoding='utf-8')
        for line in text.splitlines():
            for m in ANSWER_KEY_CELL_REGEX.finditer(line):
                qnum = int(m.group(1))
                cell = m.group(2).strip()
                if cell == WITHDRAWN_ANSWER:
                    result[qnum] = (WITHDRAWN_ANSWER, None)
                    continue
                # "A (40%)" -> letter="A", pct=40
                # "C (55)"  -> letter="C", pct=55
                # "D"       -> letter="D", pct=None
                letter_m = re.match(r'^([A-Z])(?:\s\((\d+)%?\))?$', cell)
                if letter_m:
                    letter = letter_m.group(1)
                    pct = int(letter_m.group(2)) if letter_m.group(2) else None
                    result[qnum] = (letter, pct)
        return result

    def _parse_questions(self, path: Path, year: int) -> Dict[int, Dict]:
        """Parse `<YYYY>DSE1A.md` into per-question {question_text, options}.

        Cluster preambles ("Answer Questions … with reference to …") are detected
        and stripped before splitting, so the shared context is recorded in
        ``cluster_desc`` rather than polluting the preceding question's
        ``answer_options``. The cleaned text is then split on line-starting
        question anchors ``**<NN>.``.
        """
        text = path.read_text(encoding='utf-8')
        cluster_by_qnum, cluster_desc, spans = self._detect_clusters(text)
        self.cluster_by_qnum[year] = cluster_by_qnum
        self.cluster_desc[year] = cluster_desc

        # Remove cluster preambles from the raw text (in reverse offset order so
        # earlier removals do not invalidate later indices), leaving them absent
        # from every question block.
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + text[end:]

        positions = [(m.start(), int(m.group(1))) for m in QUESTION_ANCHOR_REGEX.finditer(text)]
        result = {}
        for i, (start, qnum) in enumerate(positions):
            end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            block = text[start:end].strip()
            parts = self._split_options(block)
            result[qnum] = {
                'question_text': parts['body'],
                'answer_options_raw': parts['options'],
                'has_textual_options': parts['has_textual_options'],
            }
        return result

    def _detect_clusters(self, text: str) -> Tuple[Dict[int, int], Dict[int, str], List[Tuple[int, int]]]:
        """Detect cluster preambles and return (cluster_by_qnum, cluster_desc, spans).

        A cluster preamble is a line ``Answer Questions <nums> with reference to …``
        followed by any shared context (intro sentence, image links, code fences)
        up to the next question anchor. Every question number named in the preamble
        becomes a member of the same cluster; each cluster's description is the
        verbatim preamble text.
        """
        cluster_by_qnum: Dict[int, int] = {}
        cluster_desc: Dict[int, str] = {}
        spans: List[Tuple[int, int]] = []
        cluster_id = 0
        for m in CLUSTER_INTRO_REGEX.finditer(text):
            nums = [int(x) for x in re.findall(r'\d+', m.group(1))]
            if not nums:
                continue
            cluster_id += 1
            next_anchor = QUESTION_ANCHOR_REGEX.search(text, m.start())
            end = next_anchor.start() if next_anchor else len(text)
            desc = text[m.start():end].strip()
            for n in nums:
                cluster_by_qnum[n] = cluster_id
            cluster_desc[cluster_id] = desc
            spans.append((m.start(), end))
        return cluster_by_qnum, cluster_desc, spans

    def _split_options(self, block: str) -> Dict:
        """Split a raw question block into the question stem (``body``) and the
        textual answer-options block.

        Two option layouts are recognised:
          * Line format   — options begin at a line starting with ``A.`` (etc.),
            either followed by text on the same line or left empty with the body
            on the following line (code-snippet options, e.g. 2015 Q25).
          * Table format  — options are embedded in a Markdown table whose first
            column carries the letter (e.g. ``| A. | 11101001 | ... |``). In this
            case we walk backwards over contiguous table rows to capture the
            header and separator rows too.

        Note on code fences: we intentionally do NOT skip fenced code here. The
        option marker pattern ``^[A-D]\\.(\\s|$)`` is specific enough that
        algorithm pseudocode (``A <- 1``, ``SELECT ...``, ``If NOT (...)``) never
        matches, so scanning inside fences is safe and is required for questions
        whose options are themselves fenced code snippets (2015 Q25, 2016 Q29).
        """
        first_option_idx = None
        option_style = None  # 'line' | 'table'
        lines = block.split('\n')
        for i, line in enumerate(lines):
            if LINE_OPTION_REGEX.match(line):
                first_option_idx = i
                option_style = 'line'
                break
            if TABLE_OPTION_ROW_REGEX.match(line):
                first_option_idx = i
                option_style = 'table'
                break
        if first_option_idx is None:
            # No textual A./B./C./D. markers: either image_only modality or
            # upstream markdown error (caller decides via modality logic).
            return {
                'body': block,
                'options': '',
                'has_textual_options': False,
            }
        # For table-format options, extend upwards over contiguous table rows so
        # the header/separator rows are included in the captured options block.
        if option_style == 'table':
            j = first_option_idx
            while j - 1 >= 0 and TABLE_ROW_REGEX.match(lines[j - 1]):
                j -= 1
            first_option_idx = j
        body = '\n'.join(lines[:first_option_idx]).strip()
        options = '\n'.join(lines[first_option_idx:]).strip()
        return {
            'body': body,
            'options': options,
            'has_textual_options': True,
        }

    def _parse_crops(self, path: Path) -> Dict[int, List[Dict]]:
        """Parse `<YYYY>DSE1A_crops.json` into a dict keyed by question number.

        Rule R5 (§7.3): the manifest is the canonical source of truth; orphan
        PNG files on disk that are not listed in the manifest are ignored.
        """
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        result = {}
        for e in data['elements']:
            name = e['name']
            qnum = self._extract_qnum_from_name(name)
            if qnum is None:
                logger.debug(f'Skipping unparseable element name: {name}')
                continue
            result.setdefault(qnum, []).append(e)
        # Stable per-question ordering: page first, then element name.
        for qnum in result:
            result[qnum].sort(key=lambda r: (r['page'], r['name']))
        return result

    def _extract_qnum_from_name(self, name: str) -> Optional[int]:
        """Parse the question number from a crop-element name of the form
        ``<YYYY>DSE1A_q<NN>_<context>``. Returns ``None`` on pattern mismatch.
        """
        m = re.search(r'_q(\d{1,2})_', name)
        if m:
            return int(m.group(1))
        return None

    def _merge_all(self) -> List[Dict]:
        """Top-level merge: iterate over every question block in the per-year
        Markdown and emit exactly one output row per ``qn_id`` (§7.2).

        Orphan classifications (present in ``exam_questions.csv`` but absent
        from the Markdown) are surfaced as Rule R11 warnings and not emitted.
        """
        records = []
        markdown_qn_ids = set()

        for year in sorted(self.questions.keys()):
            for qnum in sorted(self.questions[year].keys()):
                qn_id = f'{SUBJECT}_{year}_{PAPER}_q{qnum:02d}'
                markdown_qn_ids.add(qn_id)
                rec = self._build_row(year, qnum, qn_id)
                # _build_row already logs the specific blocking rule (R8/R9)
                # before returning None, so we simply skip that row here.
                if rec is not None:
                    records.append(rec)

        # R11: orphan classifications — a qn_id in the CSV but not the Markdown.
        if self.classification_df is not None and 'qn_id' in self.classification_df.columns:
            classification_qn_ids = set(self.classification_df['qn_id'].astype(str))
            for qn_id in sorted(classification_qn_ids - markdown_qn_ids):
                self.report.warn(
                    'R11', qn_id,
                    'Orphan classification: present in exam_questions.csv but '
                    'absent from the question Markdown',
                )

        return records

    def _lookup_qn_types(self, qn_id: str) -> Optional[str]:
        """Return the ascending, de-duplicated, semicolon-joined ``qn_type_id``
        list for a question, or ``None`` if the question is not classified.
        """
        if self.classification_df is None or 'qn_id' not in self.classification_df.columns:
            return None
        rows = self.classification_df[self.classification_df['qn_id'] == qn_id]
        types = sorted(set(rows['qn_type_id'].astype(str).tolist()))
        if not types:
            return None
        return ';'.join(types)

    def _build_row(self, year: int, qnum: int, qn_id: str) -> Optional[Dict]:
        """Build a single 11-column output record for a given ``qn_id``.

        Returns ``None`` when a blocking rule (R8 / R9) fires.
        """
        q_info = self.questions[year][qnum]

        # Classification (A) — aggregate into an ordered, de-duplicated list.
        qn_type_ids = self._lookup_qn_types(qn_id)
        if qn_type_ids is None:
            self.report.warn('R1', qn_id,
                             'Question not classified under any type; '
                             f'qn_type_id set to "{UNCLASSIFIED_TOKEN}"')
            qn_type_ids = UNCLASSIFIED_TOKEN

        # Answer key (B).
        answer = self.answer_keys.get(year, {}).get(qnum)
        if answer is None:
            self.report.warn('R3', qn_id, f'Answer key missing for year {year}')
            correct_answer = ''
            cohort_pct = ''
        else:
            correct_answer, cohort_pct = answer

        # Image metadata (D).
        elements = self.crop_elements.get(year, {}).get(qnum, [])
        qn_images, ans_images = self._partition_images(elements, qn_id)

        has_textual = q_info['has_textual_options']
        has_options_image = len(ans_images) > 0

        # --- Answer-option modality classification (§2.12)
        #
        # has_textual | has_options_image | modality        | answer_options
        # ------------+-------------------+-----------------+-------------------------
        # True        | False             | text_only       | A...D text verbatim
        # True        | True              | image_plus_text | A...D text verbatim + ans_image_filename
        # False       | True              | image_only      | [IMAGE] sentinel + ans_image_filename
        # False       | False             | INVALID (R9)    | blocked
        if not has_textual and not has_options_image:
            self.report.block('R9', qn_id,
                              'Neither textual A./B./C./D. markers nor an '
                              'options-suffixed image were found.')
            return None

        if has_textual:
            answer_options = q_info['answer_options_raw']
        else:
            answer_options = IMAGE_ONLY_SENTINEL

        # Rule R8: sentinel emitted but ans_image_filename empty => data loss.
        if answer_options == IMAGE_ONLY_SENTINEL and not has_options_image:
            self.report.block('R8', qn_id,
                              'answer_options == [IMAGE] sentinel but '
                              'ans_image_filename is empty.')
            return None

        # Cluster membership.
        cluster = 0
        cluster_desc_text = ''
        cid = self.cluster_by_qnum.get(year, {}).get(qnum)
        if cid is not None:
            cluster = 1
            cluster_desc_text = self.cluster_desc.get(year, {}).get(cid, '')

        record = {
            'qn_type_id': qn_type_ids,
            'qn_id': qn_id,
            'paper_year': year,
            'question_text': q_info['question_text'],
            'qn_image_filename': self._join_strs(qn_images, 'filename'),
            'qn_image_type': self._join_strs(qn_images, 'kind'),
            'qn_image_context': self._join_strs(qn_images, 'context'),
            'ans_image_filename': self._join_strs(ans_images, 'filename'),
            'answer_options': answer_options,
            'correct_answer': correct_answer,
            # cohort_pct: empty string when not published (not 0, per §2.14).
            'cohort_pct': cohort_pct if cohort_pct is not None else '',
            'cluster': cluster,
            'cluster_desc': cluster_desc_text,
        }

        # Rule R6: warn on cardinality mismatch within the question-body family.
        self._check_cardinality(record, qn_id)
        return record

    def _partition_images(self, elements: List[Dict], qn_id: str) -> Tuple[List[Dict], List[Dict]]:
        """Dispatch crop elements to the two image families.

        Dispatch rule (§7.2 step 5):
          * Element name ends with the literal suffix ``_options`` ->
            route to the answer-option family (ans_image_filename).
          * Element name is listed in OPTIONS_IMAGE_OVERRIDES ->
            route to the answer-option family (manual correction).
          * All other suffixes -> route to the question-body family (qn_image_*).

        ``kind`` defaults to ``figure`` when absent from the manifest (§2.10 R4).
        """
        qn_images = []
        ans_images = []
        for e in elements:
            kind = e.get('kind', DEFAULT_IMAGE_KIND)
            context = self._extract_context_from_name(e['name'])
            entry = {
                'name': e['name'],
                'kind': kind,
                'context': context,
                'filename': self._build_image_filename(e['name']),
            }
            is_options_image = (
                e['name'].endswith(OPTIONS_SUFFIX)
                or e['name'] in OPTIONS_IMAGE_OVERRIDES
            )
            if is_options_image:
                ans_images.append(entry)
            else:
                qn_images.append(entry)
        return qn_images, ans_images

    def _extract_context_from_name(self, name: str) -> str:
        """Return the descriptive suffix of a crop-element name.

        Example: ``2021DSE1A_q14_table`` -> ``table``.
        """
        m = re.match(r'^\d{4}DSE1A_q\d{1,2}_(.+)$', name)
        return m.group(1) if m else ''

    def _build_image_filename(self, element_name: str) -> str:
        """Construct the forward-slash-separated relative path written into the
        image filename cells of the CSV.

        Path format: ``question/<YYYY>/<YYYY>DSE1A_images/<element_name>.png``
        """
        year_match = re.match(r'^(\d{4})DSE1A_', element_name)
        year = year_match.group(1) if year_match else ''
        return f'question/{year}/{year}DSE1A_images/{element_name}.png'

    def _join_strs(self, items: List[Dict], key: str) -> str:
        """Join a list of element-entries' ``key`` values with ``;`` separators.

        Empty list -> empty string (consistent with "nullable / empty value
        permitted" in the field definitions).
        """
        if not items:
            return ''
        return ';'.join(str(item[key]) for item in items)

    def _extract_qnum_from_qn_id(self, qn_id: str) -> Optional[int]:
        """Return the question number embedded in ``qn_id`` (second capture
        group of QN_ID_REGEX). ``None`` when regex fails.
        """
        m = QN_ID_REGEX.match(qn_id)
        if m:
            return int(m.group(2))
        return None

    def _check_cardinality(self, record: Dict, qn_id: str):
        """Rule R6 warning: ensure the three question-body image cells have
        matching semicolon-delimited lengths.
        """
        prefix = 'qn_image'
        f = record.get(f'{prefix}_filename', '')
        ty = record.get(f'{prefix}_type', '')
        cx = record.get(f'{prefix}_context', '')
        lens = [
            len(f.split(';')) if f else 0,
            len(ty.split(';')) if ty else 0,
            len(cx.split(';')) if cx else 0,
        ]
        if len(set(lens)) > 1:
            self.report.warn('R6', qn_id,
                             f'{prefix} family cardinality mismatch: '
                             f'filename[{lens[0]}] / type[{lens[1]}] / context[{lens[2]}]')

    def _validate(self, records: List[Dict]) -> bool:
        """Strict-mode validation of all built rows against the invariants in
        §8 of final_capstone_flow.md.

        Returns ``True`` if NO blocking violations are found (warnings are OK);
        returns ``False`` when at least one row is blocked.
        """
        ok = True
        seen = set()
        for rec in records:
            qn_id = rec['qn_id']

            # I1 (§8): qn_id must be unique (one row per question).
            if qn_id in seen:
                self.report.block('R-DUPLICATE', qn_id, 'Duplicate qn_id in output')
                ok = False
            seen.add(qn_id)

            # I2 (§8): qn_id regex must match.
            if not QN_ID_REGEX.match(qn_id):
                self.report.block('R-INVALID-QN-ID', qn_id,
                                  'qn_id regex mismatch during validate()')
                ok = False

            # I3 (§8): correct_answer is a single letter A–Z or '*'.
            ca = rec['correct_answer']
            if not (re.fullmatch(r'[A-Z]', ca) or ca == WITHDRAWN_ANSWER):
                self.report.block('R-INVALID-ANSWER', qn_id,
                                  f'correct_answer invalid: {ca!r}')
                ok = False

            # qn_type_id must never be empty.
            if not rec['qn_type_id']:
                self.report.block('R-EMPTY-TYPE', qn_id, 'qn_type_id is empty')
                ok = False

            # cluster ∈ {0, 1}; cluster==1 ⇒ cluster_desc must be non-empty.
            if rec['cluster'] not in (0, 1):
                self.report.block('R-CLUSTER', qn_id,
                                  f'cluster invalid: {rec["cluster"]!r}')
                ok = False
            if rec['cluster'] == 1 and not rec['cluster_desc']:
                self.report.block('R-CLUSTER-DESC', qn_id,
                                  'cluster=1 but cluster_desc is empty')
                ok = False

            # I6 (§8): answer_options is never empty.
            ans_opts = rec['answer_options']
            if not ans_opts:
                self.report.block('R9', qn_id,
                                  'answer_options is empty at validate() stage')
                ok = False
            elif ans_opts == IMAGE_ONLY_SENTINEL:
                # sentinel row must have non-empty ans_image_filename.
                if not rec['ans_image_filename']:
                    self.report.block('R8', qn_id,
                                      '[IMAGE] sentinel with empty ans_image_filename')
                    ok = False
            else:
                # Best-effort regression guard: a non-sentinel answer_options
                # block must begin as a line-format option or a Markdown table.
                stripped = ans_opts.lstrip()
                if not (stripped.startswith('A.') or stripped.startswith('|')):
                    self.report.warn('R-WARN-OPTIONS', qn_id,
                                     'answer_options does not start with "A." or "|"')
        return ok

    def _write_output(self, records: List[Dict], output_path: str):
        """Write the list of records as an 11-column CSV in canonical order.

        Sort order matches §7.4: (paper_year, qn_id).
        """
        df = pd.DataFrame(records)
        df = df.reindex(columns=OUTPUT_COLUMNS)
        df = df.sort_values(by=['paper_year', 'qn_id']).reset_index(drop=True)
        df.to_csv(output_path, index=False)
        logger.info(f'Wrote {len(df)} rows to {output_path}')

    def _write_report(self, report_path: str):
        """Persist the MergeReport findings as a JSON document."""
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(self.report.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f'Wrote validation report to {report_path}')


def main():
    """Parse CLI arguments, build the DatasetMerger, and run the pipeline.

    Returns a process exit code (0 for success, 1 for failure).
    """
    # Sensible defaults: prefer the /data mount (used in containers), else
    # fall back to the current working directory.
    default_root = '/data'
    default_output = 'final_dataset.csv'
    default_report = 'validation_report.json'

    if not os.path.exists(default_root):
        default_root = '.'

    parser = argparse.ArgumentParser(
        description='Stage 4: Merge exam_questions.csv + answer/*.md '
                    '+ question/<YYYY>/* into an 11-column final_dataset.csv '
                    'per fields_explanation.md v1.5 / final_capstone_flow.md v1.4',
    )
    parser.add_argument('--data-root', '-d', default=default_root,
                        help=f'Root data directory (default: {default_root})')
    parser.add_argument('--output', '-o', default=default_output,
                        help=f'Output CSV path (default: {default_output})')
    parser.add_argument('--report', '-r', default=default_report,
                        help=f'Validation report JSON path (default: {default_report})')

    args = parser.parse_args()

    if not Path(args.data_root).exists():
        logger.error(f'Data root not found: {args.data_root}')
        return 1

    merger = DatasetMerger(args.data_root)
    success = merger.run(args.output, args.report)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
