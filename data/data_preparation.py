"""
Data Preparation Script for DSE ICT Exam Question Classification
GENERATES PROPERLY FORMATTED qn_id

Purpose:
    - Parses DSE_MC_Classification.xlsx to extract question classifications
    - Generates qn_id in proper format: ICT_YEAR_paper1a_qNN
    - Creates CSV with correctly formatted fields per fields_explanation.md
    - Ready to be merged with actual exam question text later

Usage:
    python data_preparation.py --classification DSE_MC_Classification.xlsx --output exam_questions.csv

Output Columns:
    - qn_type_id: Question type ID (e.g., 'a_ip_01', 'b_cs_03')
    - qn_id: Question ID (e.g., 'ICT_2021_paper1a_q01')
    - paper_year: Year of exam (2012-2024)

Author: Stanley Lo
Date: 2026-09-30
Modified: Generate properly formatted qn_id per fields_explanation.md
"""

import pandas as pd
import numpy as np
from pathlib import Path
import argparse
import logging
from typing import Dict, List, Tuple
import sys
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DSEClassificationExtractor:
    """Extract question classifications from DSE_MC_Classification.xlsx."""
    
    def __init__(self, classification_file: str):
        """
        Initialize with classification file.
        
        Args:
            classification_file: Path to DSE_MC_Classification.xlsx
        """
        self.classification_file = classification_file
        self.index_df = None
        self.core_sheets = {}  # {sheet_name: dataframe}
        self.qn_type_mapping = {}  # {qn_type_id: metadata}
        self.years = []
        
        # Constants per fields_explanation.md
        self.SUBJECT = 'ICT'  # All questions are from ICT
        self.PAPER = 'paper1a'  # All questions are from Paper 1A
        
        logger.info(f"Initializing with classification file: {classification_file}")
        logger.info(f"Format per fields_explanation.md: {self.SUBJECT}_{{year}}_{self.PAPER}_q{{qn:02d}}")
    
    def load_index_sheet(self) -> bool:
        """
        Load the index sheet which contains qn_type_id mappings.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            self.index_df = pd.read_excel(self.classification_file, sheet_name='index')
            logger.info(f"Loaded index sheet: {len(self.index_df)} rows")
            logger.info(f"Columns: {list(self.index_df.columns)}")
            
            # Build mapping of qn_type_id to metadata
            for _, row in self.index_df.iterrows():
                qn_type_id = str(row['qn_type_id']).strip().lower()
                self.qn_type_mapping[qn_type_id] = {
                    'core_type': str(row['core_type']).strip(),
                    'cat_id': str(row['cat_id']).strip() if pd.notna(row.get('cat_id')) else '',
                    'cat_name': str(row['cat_name']).strip() if pd.notna(row.get('cat_name')) else '',
                    'subcat_id': str(row['subcat_id']).strip() if pd.notna(row.get('subcat_id')) else '',
                    'subcat_name': str(row['subcat_name']).strip() if pd.notna(row.get('subcat_name')) else ''
                }
            
            logger.info(f"Built mapping with {len(self.qn_type_mapping)} question types")
            return True
        
        except Exception as e:
            logger.error(f"Failed to load index sheet: {e}")
            return False
    
    def load_core_sheets(self) -> bool:
        """
        Load all core group sheets (IP, CS, IA, CP, SI).
        
        Returns:
            True if successful, False otherwise
        """
        try:
            xls = pd.ExcelFile(self.classification_file)
            sheet_names = xls.sheet_names
            
            # Skip 'index', process the core sheets
            core_sheet_names = [s for s in sheet_names if s != 'index']
            
            for sheet_name in core_sheet_names:
                df = pd.read_excel(self.classification_file, sheet_name=sheet_name)
                self.core_sheets[sheet_name] = df
                
                # Extract years from 'Year' column
                if 'Year' in df.columns:
                    years_in_sheet = sorted(df['Year'].dropna().astype(int).unique().tolist())
                    self.years.extend(years_in_sheet)
                
                logger.info(f"Loaded {sheet_name}: {len(df)} rows, columns: {list(df.columns)}")
            
            self.years = sorted(set(self.years))
            logger.info(f"Years covered: {self.years}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to load core sheets: {e}")
            return False
    
    def parse_question_numbers(self, cell_value) -> List[int]:
        """
        Parse comma-separated question numbers from a cell.
        
        Handles formats like: "1", "1,2,3", "1, 2, 3", "1.0", etc.
        
        Args:
            cell_value: Raw cell value (can be NaN, int, float, or string)
        
        Returns:
            List of question numbers (int), sorted and deduplicated
        """
        if pd.isna(cell_value):
            return []
        
        cell_value = str(cell_value).strip()
        if not cell_value:
            return []
        
        question_nums = []
        for item in cell_value.split(','):
            item = item.strip()
            try:
                # Convert to float first to handle "1.0", then int
                qnum = int(float(item))
                question_nums.append(qnum)
            except ValueError:
                logger.warning(f"Could not parse question number: '{item}'")
                continue
        
        return sorted(set(question_nums))  # Remove duplicates, sort
    
    def format_qn_id(self, year: int, question_number: int) -> str:
        """
        Format question classification ID per fields_explanation.md.
        
        Format: [subject]_[year]_[papernumber]_[questionnumber]
        Example: ICT_2021_paper1a_q01
        
        Args:
            year: Exam year (e.g., 2021)
            question_number: Question number (1-40)
        
        Returns:
            Formatted qn_id string
        """
        return f"{self.SUBJECT}_{year}_{self.PAPER}_q{question_number:02d}"
    
    def extract_classifications(self) -> pd.DataFrame:
        """
        Extract all question classifications from core sheets.
        
        Returns:
            DataFrame with columns:
                - qn_type_id (str): Question type ID (e.g., 'a_ip_01')
                - qn_id (str): Question ID (e.g., 'ICT_2021_paper1a_q01')
                - paper_year (int): Exam year (2012-2024)
        """
        records = []
        
        for sheet_name, sheet_df in self.core_sheets.items():
            logger.info(f"Processing sheet: {sheet_name}")
            
            # Get year column
            if 'Year' not in sheet_df.columns:
                logger.warning(f"  No 'Year' column in {sheet_name}, skipping...")
                continue
            
            # Iterate through each row (each year)
            for _, row in sheet_df.iterrows():
                year = int(row['Year'])
                
                # Iterate through each qn_type_id column
                for col in sheet_df.columns:
                    if col == 'Year':
                        continue
                    
                    qn_type_id = col.lower()  # Normalize to lowercase
                    cell_value = row[col]
                    
                    # Parse question numbers from cell
                    question_nums = self.parse_question_numbers(cell_value)
                    
                    # Create record for each question
                    for qnum in question_nums:
                        qn_id = self.format_qn_id(year, qnum)
                        records.append({
                            'qn_type_id': qn_type_id,
                            'qn_id': qn_id,
                            'paper_year': year
                        })
        
        result_df = pd.DataFrame(records)
        logger.info(f"Extracted {len(result_df)} question classifications")
        
        return result_df
    
    def validate_dataset(self, df: pd.DataFrame) -> bool:
        """
        Validate extracted dataset quality.
        
        Args:
            df: Dataset to validate
        
        Returns:
            True if valid, False otherwise
        """
        logger.info("Validating dataset...")
        
        issues = []
        
        # Check required columns
        required_cols = ['qn_type_id', 'qn_id', 'paper_year']
        for col in required_cols:
            if col not in df.columns:
                issues.append(f"Missing required column: {col}")
        
        # Check for null values
        for col in required_cols:
            if col in df.columns:
                nulls = df[col].isna().sum()
                if nulls > 0:
                    issues.append(f"{col}: {nulls} null values")
        
        # Check qn_id format
        if 'qn_id' in df.columns:
            invalid_format = 0
            for qn_id in df['qn_id']:
                # Should be format: ICT_YYYY_paper1a_qNN
                if not (isinstance(qn_id, str) and qn_id.startswith('ICT_') and '_q' in qn_id):
                    invalid_format += 1
            if invalid_format > 0:
                issues.append(f"qn_id: {invalid_format} invalid format")
        
        # Check year range
        if 'paper_year' in df.columns:
            min_year, max_year = df['paper_year'].min(), df['paper_year'].max()
            logger.info(f"Year range: {min_year}-{max_year}")
            
            if not (2012 <= min_year <= 2024 and 2012 <= max_year <= 2024):
                issues.append(f"Years out of expected range (2012-2024): {min_year}-{max_year}")
        
        # Check qn_type_id distribution
        if 'qn_type_id' in df.columns:
            qn_type_counts = df['qn_type_id'].value_counts()
            logger.info(f"Question types: {len(qn_type_counts)} types")
            logger.info(f"Distribution:\n{qn_type_counts.head(10)}")
        
        if issues:
            logger.error("❌ Validation FAILED:")
            for issue in issues:
                logger.error(f"  - {issue}")
            return False
        else:
            logger.info("✅ Validation PASSED")
            return True
    
    def save_dataset(self, df: pd.DataFrame, output_file: str) -> bool:
        """
        Save extracted classifications to CSV.
        
        Args:
            df: Dataset to save
            output_file: Output file path
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Sort by paper_year and qn_id for consistency
            df_sorted = df.sort_values(by=['paper_year', 'qn_type_id', 'qn_id']).reset_index(drop=True)
            
            df_sorted.to_csv(output_file, index=False)
            logger.info(f"✅ Dataset saved to: {output_file}")
            logger.info(f"   Rows: {len(df_sorted)}, Columns: {len(df_sorted.columns)}")
            
            # Print sample rows
            logger.info("\nFirst 10 rows of output:")
            print("\n" + df_sorted.head(10).to_string())
            
            logger.info("\nLast 10 rows of output:")
            print("\n" + df_sorted.tail(10).to_string())
            
            # Show sample qn_id values
            logger.info("\nSample qn_id values:")
            samples = df_sorted['qn_id'].drop_duplicates().head(10)
            for sample in samples:
                logger.info(f"  {sample}")
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to save dataset: {e}")
            return False
    
    def run(self, output_file: str) -> bool:
        """
        Execute full extraction pipeline.
        
        Args:
            output_file: Output file path (exam_questions.csv)
        
        Returns:
            True if successful, False otherwise
        """
        logger.info("=" * 70)
        logger.info("DSE CLASSIFICATION EXTRACTION PIPELINE")
        logger.info("Format per fields_explanation.md")
        logger.info("=" * 70)
        
        # Step 1: Load index sheet
        if not self.load_index_sheet():
            return False
        
        # Step 2: Load core sheets
        if not self.load_core_sheets():
            return False
        
        # Step 3: Extract classifications
        classification_df = self.extract_classifications()
        
        if len(classification_df) == 0:
            logger.error("No classifications extracted!")
            return False
        
        # Step 4: Validate
        if not self.validate_dataset(classification_df):
            logger.warning("⚠️  Dataset validation failed, but continuing...")
        
        # Step 5: Save
        if not self.save_dataset(classification_df, output_file):
            return False
        
        logger.info("=" * 70)
        logger.info("✅ EXTRACTION COMPLETE")
        logger.info("=" * 70)
        logger.info("\nOutput format (per fields_explanation.md):")
        logger.info(f"  qn_type_id: e.g., a_ip_01 (question type)")
        logger.info(f"  qn_id: e.g., ICT_2021_paper1a_q01 (fully formatted)")
        logger.info(f"  paper_year: e.g., 2021")
        logger.info("\nNext Steps:")
        logger.info("1. Verify qn_id format matches specification")
        logger.info("2. Obtain actual exam question text for each qn_id")
        logger.info("3. Merge with this CSV to add question content")
        logger.info("4. Use in notebook for model training")
        
        return True


def main():
    """Command-line interface."""
    # Default to /data directory if it exists
    default_data_dir = '/data'
    default_classification = os.path.join(default_data_dir, 'DSE_MC_Classification.xlsx')
    default_output = os.path.join(default_data_dir, 'exam_questions.csv')
    
    # Fall back to current directory if /data doesn't exist
    if not os.path.exists(default_data_dir):
        default_classification = 'DSE_MC_Classification.xlsx'
        default_output = 'exam_questions.csv'
        logger.info(f"⚠️  /data directory not found. Using current directory as default.")
    else:
        logger.info(f"✅ Using /data directory for default paths")
    
    parser = argparse.ArgumentParser(
        description='Extract DSE question classifications with proper formatting',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
OUTPUT FORMAT (per fields_explanation.md):
  qn_id: [subject]_[year]_[papernumber]_[questionnumber]
  Example: ICT_2021_paper1a_q01
  
DEFAULTS:
  Classification: {default_classification}
  Output: {default_output}
"""
    )
    
    parser.add_argument(
        '--classification',
        '-c',
        default=default_classification,
        help=f'Path to DSE_MC_Classification.xlsx (default: {default_classification})'
    )
    
    parser.add_argument(
        '--output',
        '-o',
        default=default_output,
        help=f'Output file path (default: {default_output})'
    )
    
    args = parser.parse_args()
    
    # Check file exists
    if not Path(args.classification).exists():
        logger.error(f"Classification file not found: {args.classification}")
        return 1
    
    # Create output directory if needed
    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
            logger.info(f"Created output directory: {output_dir}")
        except Exception as e:
            logger.error(f"Failed to create output directory: {e}")
            return 1
    
    # Run pipeline
    extractor = DSEClassificationExtractor(args.classification)
    success = extractor.run(args.output)
    
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
