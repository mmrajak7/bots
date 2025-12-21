"""
SNAIL Database Cleanup Utility

Clean up and reset the database for a fresh start.

@file        db_cleanup.py
@description Database cleanup and reset utilities
@author      SNAIL Development Team
@created     2025-12-20
"""

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger


# Valid table names (whitelist for SQL safety)
VALID_TABLES = frozenset([
    'positions', 'position_legs', 'orders', 'pnl_snapshots',
    'claude_decisions', 'alert_queue', 'response_queue',
    'alert_cooldowns', 'cooldowns', 'entry_attempts',
    'system_status', 'market_data'
])


def backup_database(db_path: Path) -> Optional[Path]:
    """
    Create a backup of the current database.

    Args:
        db_path: Path to the database file

    Returns:
        Path to the backup file
    """
    if not db_path.exists():
        logger.warning(f"Database does not exist: {db_path}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"snail_backup_{timestamp}.db"

    shutil.copy2(db_path, backup_path)
    logger.info(f"Database backed up to: {backup_path}")

    return backup_path


def get_database_stats(db_path: Path) -> Dict[str, Any]:
    """
    Get statistics about the current database.

    Args:
        db_path: Path to the database file

    Returns:
        Dictionary with table counts and stats
    """
    if not db_path.exists():
        return {"error": "Database does not exist"}

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        stats: Dict[str, Any] = {}

        for table in VALID_TABLES:
            try:
                # Table name is from whitelist, safe to use
                cursor.execute(f"SELECT COUNT(*) FROM {table}")  # nosec B608
                row = cursor.fetchone()
                stats[table] = row[0] if row else 0
            except sqlite3.Error:
                stats[table] = 0

        return stats

    except sqlite3.Error as e:
        logger.error(f"Database error getting stats: {e}")
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()


def clear_all_data(db_path: Path, schema_path: Path) -> bool:
    """
    Clear all data and recreate database from schema.

    Args:
        db_path: Path to the database file
        schema_path: Path to schema.sql file

    Returns:
        True if successful
    """
    try:
        # Backup first
        if db_path.exists():
            backup_database(db_path)

            # Delete the old database
            os.remove(db_path)
            logger.info(f"Deleted old database: {db_path}")

        # Create new database from schema
        if not schema_path.exists():
            logger.error(f"Schema file not found: {schema_path}")
            return False

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        with open(schema_path, 'r') as f:
            schema_sql = f.read()

        cursor.executescript(schema_sql)
        conn.commit()
        conn.close()

        logger.info(f"Database recreated from schema: {db_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to clear database: {e}")
        return False


def cleanup_old_data(db_path: Path, keep_days: int = 0) -> Dict[str, Any]:
    """
    Clean up old data while keeping recent records.

    Args:
        db_path: Path to the database file
        keep_days: Number of days of data to keep (0 = delete all)

    Returns:
        Dictionary with deleted record counts
    """
    if not db_path.exists():
        return {"error": "Database does not exist"}

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        deleted: Dict[str, Any] = {}

        if keep_days == 0:
            # Delete order matters for foreign key constraints
            # Tables ordered by dependency (children first)
            tables_to_clear = [
                'pnl_snapshots', 'claude_decisions', 'alert_queue',
                'response_queue', 'alert_cooldowns', 'orders',
                'position_legs', 'positions', 'cooldowns',
                'entry_attempts', 'market_data'
            ]

            for table in tables_to_clear:
                if table not in VALID_TABLES:
                    logger.warning(f"Skipping unknown table: {table}")
                    continue
                try:
                    # Table name is from whitelist, safe to use
                    cursor.execute(f"DELETE FROM {table}")  # nosec B608
                    deleted[table] = cursor.rowcount
                except sqlite3.Error as e:
                    logger.warning(f"Could not clear {table}: {e}")
                    deleted[table] = 0

            # Reset system status to normal
            cursor.execute("DELETE FROM system_status")
            cursor.execute("""
                INSERT INTO system_status (id, status, reason, set_by)
                VALUES (1, 'normal', 'Database cleaned - fresh start', 'system')
            """)
            deleted['system_status'] = 'reset'

        conn.commit()
        return deleted

    except sqlite3.Error as e:
        logger.error(f"Database cleanup error: {e}")
        if conn:
            conn.rollback()
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()


def run_cleanup(project_root: Optional[Path] = None) -> Dict[str, Any]:
    """
    Run full database cleanup.

    Args:
        project_root: Project root directory (auto-detected if None)

    Returns:
        Dictionary with cleanup results
    """
    if project_root is None:
        project_root = Path(__file__).parent.parent.parent

    db_path = project_root / "data" / "snail.db"
    schema_path = project_root / "data" / "schema.sql"

    results: Dict[str, Any] = {
        "db_path": str(db_path),
        "before": {},
        "after": {},
        "success": False
    }

    # Get stats before cleanup
    results["before"] = get_database_stats(db_path)

    # Run cleanup
    if clear_all_data(db_path, schema_path):
        results["success"] = True
        results["after"] = get_database_stats(db_path)

    return results


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(" SNAIL Database Cleanup")
    print("=" * 60)

    results = run_cleanup()

    print(f"\nDatabase: {results['db_path']}")

    print("\nBEFORE cleanup:")
    for table, count in results['before'].items():
        print(f"  {table}: {count}")

    print(f"\nCleanup {'SUCCESSFUL' if results['success'] else 'FAILED'}")

    print("\nAFTER cleanup:")
    for table, count in results['after'].items():
        print(f"  {table}: {count}")

    print("\n" + "=" * 60)
