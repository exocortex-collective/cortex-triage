#!/usr/bin/env python3
"""One-time migration script: Import rules.yaml to database.

Usage:
    python -m triage.scripts.migrate_rules \
        --rules-file ~/HomeLab/cortex/triage/rules.yaml \
        --created-by migration@system
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2

from triage.db.config_loader import (
    ConfigImportError,
    export_config_to_yaml,
    import_yaml_to_db,
)
from triage.engine.rules import load_rules_from_string

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate rules.yaml to database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Migrate rules to database
    python -m triage.scripts.migrate_rules \\
        --rules-file ~/HomeLab/cortex/triage/rules.yaml \\
        --created-by migration@system

    # Dry run (verify only)
    python -m triage.scripts.migrate_rules \\
        --rules-file ~/HomeLab/cortex/triage/rules.yaml \\
        --created-by migration@system \\
        --dry-run

Environment variables:
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
        """,
    )

    parser.add_argument(
        "--rules-file",
        required=True,
        help="Path to rules.yaml file to migrate",
    )

    parser.add_argument(
        "--created-by",
        required=True,
        help="User/system creating this version (e.g., migration@system, devon@manual)",
    )

    parser.add_argument(
        "--notes",
        help="Optional notes about this migration",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only, don't write to database",
    )

    parser.add_argument(
        "--skip-roundtrip",
        action="store_true",
        help="Skip roundtrip verification (faster, but less thorough)",
    )

    return parser.parse_args()


def get_db_connection() -> psycopg2.extensions.connection:
    """Create database connection from environment variables."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db = os.getenv("POSTGRES_DB", "cortex")
    user = os.getenv("POSTGRES_USER", "cortex")
    password = os.getenv("POSTGRES_PASSWORD")

    if not password:
        logger.error("POSTGRES_PASSWORD environment variable not set")
        sys.exit(1)

    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=db,
            user=user,
            password=password,
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        sys.exit(1)


def verify_roundtrip(
    conn: psycopg2.extensions.connection, version: int, original_yaml: str
) -> bool:
    """Verify that export → import produces identical config.

    Args:
        conn: Database connection
        version: Config version to export
        original_yaml: Original YAML content

    Returns:
        True if roundtrip successful, False otherwise
    """
    logger.info("Verifying roundtrip (export → re-parse)...")

    try:
        # Export from database
        exported_yaml = export_config_to_yaml(conn, version)

        # Re-parse both
        original_config = load_rules_from_string(original_yaml)
        exported_config = load_rules_from_string(exported_yaml)

        # Compare chain counts
        if len(original_config.chains) != len(exported_config.chains):
            logger.error(
                f"Chain count mismatch: original={len(original_config.chains)}, "
                f"exported={len(exported_config.chains)}"
            )
            return False

        # Compare rule counts per chain
        for chain_name, original_rules in original_config.chains.items():
            if chain_name not in exported_config.chains:
                logger.error(f"Missing chain in export: {chain_name}")
                return False

            exported_rules = exported_config.chains[chain_name]
            if len(original_rules) != len(exported_rules):
                logger.error(
                    f"Rule count mismatch in chain '{chain_name}': "
                    f"original={len(original_rules)}, exported={len(exported_rules)}"
                )
                return False

        # Compare email mappings counts
        if len(original_config.priority_email_mappings) != len(
            exported_config.priority_email_mappings
        ):
            logger.error(
                f"Priority mapping count mismatch: "
                f"original={len(original_config.priority_email_mappings)}, "
                f"exported={len(exported_config.priority_email_mappings)}"
            )
            return False

        if len(original_config.fallback_email_mappings) != len(
            exported_config.fallback_email_mappings
        ):
            logger.error(
                f"Fallback mapping count mismatch: "
                f"original={len(original_config.fallback_email_mappings)}, "
                f"exported={len(exported_config.fallback_email_mappings)}"
            )
            return False

        logger.info("✓ Roundtrip verification successful")
        logger.info(f"  Chains: {len(original_config.chains)}")
        logger.info(
            f"  Priority mappings: {len(original_config.priority_email_mappings)}"
        )
        logger.info(
            f"  Fallback mappings: {len(original_config.fallback_email_mappings)}"
        )

        # Count total rules
        total_rules = sum(len(rules) for rules in original_config.chains.values())
        logger.info(f"  Total rules: {total_rules}")

        return True

    except Exception as e:
        logger.error(f"Roundtrip verification failed: {e}")
        return False


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Read rules file
    rules_path = Path(args.rules_file).expanduser()
    if not rules_path.exists():
        logger.error(f"Rules file not found: {rules_path}")
        sys.exit(1)

    logger.info(f"Reading rules from {rules_path}")
    yaml_content = rules_path.read_text()

    # Validate YAML
    try:
        config = load_rules_from_string(yaml_content)
        logger.info("✓ Parsed rules successfully")
        logger.info(f"  Chains: {len(config.chains)}")
        logger.info(f"  Priority mappings: {len(config.priority_email_mappings)}")
        logger.info(f"  Fallback mappings: {len(config.fallback_email_mappings)}")

        # Count total rules
        total_rules = sum(len(rules) for rules in config.chains.values())
        logger.info(f"  Total rules: {total_rules}")

    except Exception as e:
        logger.error(f"Failed to parse rules: {e}")
        sys.exit(1)

    if args.dry_run:
        logger.info("Dry run complete (validation only)")
        return

    # Connect to database
    conn = get_db_connection()
    logger.info(
        f"Connected to database: "
        f"{os.getenv('POSTGRES_HOST')}/{os.getenv('POSTGRES_DB')}"
    )

    try:
        # Import to database
        version = import_yaml_to_db(
            conn,
            yaml_content,
            created_by=args.created_by,
            notes=args.notes,
        )

        logger.info(f"✓ Imported config as version {version}")

        # Verify roundtrip
        if not args.skip_roundtrip:
            success = verify_roundtrip(conn, version, yaml_content)
            if not success:
                logger.error("Roundtrip verification failed!")
                sys.exit(1)

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"SUCCESS: Migration complete (version {version})")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Next steps:")
        logger.info("1. Verify active config:")
        logger.info(
            "   SELECT version, is_active, created_at FROM triage_config_versions;"
        )
        logger.info("")
        logger.info("2. Update workers to use database config (see cortex-ios ticket)")
        logger.info("")

    except ConfigImportError as e:
        logger.error(f"Import failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
