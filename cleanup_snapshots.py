#!/usr/bin/env python3
"""
One-off cleanup script to fix corrupted data in snapshots.jsonl.

Fixes:
1. Pimblett vs Saint Denis rows with wrong result (Farid Basharat)
2. Bolaños vs Aswell rows with wrong result (Levan Chokheli)

Clears actual_result, pnl_zmw, and resolution fields to force resolver retry.
"""

import json
import sys
from pathlib import Path


def cleanup_snapshots(filepath: str = "snapshots.jsonl") -> None:
    """
    Clean up corrupted data in snapshots.jsonl file.
    
    Args:
        filepath: Path to the snapshots.jsonl file
    """
    snapshots_path = Path(filepath)
    
    if not snapshots_path.exists():
        print(f"Error: File '{filepath}' not found")
        sys.exit(1)
    
    # Statistics for summary
    pimblett_fixed = 0
    bolanos_fixed = 0
    
    # Read all records
    records = []
    try:
        with open(snapshots_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    records.append(record)
                except json.JSONDecodeError as e:
                    print(f"Warning: Skipping line {line_num} - invalid JSON: {e}")
                    continue
    except IOError as e:
        print(f"Error reading file: {e}")
        sys.exit(1)
    
    # Apply fixes
    for record in records:
        question = record.get('question', '').lower()
        actual_result = record.get('actual_result', '')
        
        # Fix 1: Pimblett vs Saint Denis with Farid Basharat result
        if ('pimblett' in question and 'saint denis' in question and 
            actual_result == 'Farid Basharat'):
            record['actual_result'] = ''
            record['pnl_zmw'] = ''
            record['resolution'] = {}
            pimblett_fixed += 1
        
        # Fix 2: Bolaños vs Aswell with Levan Chokheli result
        elif (('bolaños' in question or 'bolanos' in question) and 
              actual_result == 'Levan Chokheli'):
            record['actual_result'] = ''
            record['pnl_zmw'] = ''
            record['resolution'] = {}
            bolanos_fixed += 1
    
    # Write cleaned records back to file
    try:
        with open(snapshots_path, 'w', encoding='utf-8') as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except IOError as e:
        print(f"Error writing file: {e}")
        sys.exit(1)
    
    # Print summary
    print("\n" + "=" * 60)
    print("CLEANUP SUMMARY")
    print("=" * 60)
    print(f"Pimblett vs Saint Denis (Farid Basharat) fixed: {pimblett_fixed}")
    print(f"Bolaños vs Aswell (Levan Chokheli) fixed:      {bolanos_fixed}")
    print(f"Total records fixed:                            {pimblett_fixed + bolanos_fixed}")
    print(f"Total records processed:                        {len(records)}")
    print("=" * 60)
    print(f"\nCleaned data written to: {filepath}")


if __name__ == '__main__':
    filepath = sys.argv[1] if len(sys.argv) > 1 else 'snapshots.jsonl'
    cleanup_snapshots(filepath)
