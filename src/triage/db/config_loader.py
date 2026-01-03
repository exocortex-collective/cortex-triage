"""Config loader - YAML import/export for triage config database."""

import hashlib
import json
import logging
from typing import Any

import psycopg2
import yaml

from triage.engine.rules import load_rules_from_string, validate_rules
from triage.models.rules import RulesConfig

logger = logging.getLogger(__name__)


class ConfigLoadError(Exception):
    """Raised when config cannot be loaded from database."""

    pass


class ConfigImportError(Exception):
    """Raised when YAML import fails."""

    pass


def load_config_from_db(
    conn: psycopg2.extensions.connection, version: int | None = None
) -> RulesConfig:
    """Load triage config from database and convert to RulesConfig.

    Args:
        conn: Database connection
        version: Config version to load (default: active version)

    Returns:
        RulesConfig object

    Raises:
        ConfigLoadError: If config cannot be loaded
    """
    try:
        # Export DB to YAML, then parse with existing loader
        yaml_content = export_config_to_yaml(conn, version)
        return load_rules_from_string(yaml_content)
    except Exception as e:
        raise ConfigLoadError(f"Failed to load config from database: {e}") from e


def export_config_to_yaml(
    conn: psycopg2.extensions.connection, version: int | None = None
) -> str:
    """Export triage config from database to YAML string.

    Args:
        conn: Database connection
        version: Config version to export (default: active version)

    Returns:
        YAML string representation of config

    Raises:
        ConfigLoadError: If config version not found
    """
    cursor = conn.cursor()

    try:
        # Get config version
        if version is None:
            cursor.execute(
                "SELECT version FROM triage_config_versions WHERE is_active = TRUE"
            )
        else:
            cursor.execute(
                "SELECT version FROM triage_config_versions WHERE version = %s",
                (version,),
            )

        row = cursor.fetchone()
        if not row:
            raise ConfigLoadError(
                f"Config version {version if version else 'active'} not found"
            )
        version = row[0]

        # Get config metadata
        cursor.execute(
            """
            SELECT
                label_prefix,
                intents,
                email_categories,
                prompts,
                body_extraction_prompts
            FROM triage_config_versions
            WHERE version = %s
            """,
            (version,),
        )
        row = cursor.fetchone()
        label_prefix, intents, email_categories, prompts, body_extraction_prompts = row

        # Build chains dict by traversing linked lists
        chains: dict[str, list[dict[str, Any]]] = {}

        cursor.execute(
            """SELECT id, chain_name FROM triage_chains
               WHERE config_version = %s ORDER BY display_order""",
            (version,),
        )
        chain_rows = cursor.fetchall()

        for chain_id, chain_name in chain_rows:
            # Traverse linked list to get rules in order
            rules = _traverse_chain(cursor, chain_id)
            chains[chain_name] = [_rule_to_dict(r) for r in rules]

        # Fetch email mappings
        cursor.execute(
            """
            SELECT email_address, label, archive, mark_read
            FROM triage_email_mappings
            WHERE config_version = %s AND mapping_type = 'priority'
            ORDER BY email_address
            """,
            (version,),
        )
        priority_mappings = {}
        for email, label, archive, mark_read in cursor.fetchall():
            priority_mappings[email] = _build_mapping_dict(label, archive, mark_read)

        cursor.execute(
            """
            SELECT email_address, label, archive, mark_read
            FROM triage_email_mappings
            WHERE config_version = %s AND mapping_type = 'fallback'
            ORDER BY email_address
            """,
            (version,),
        )
        fallback_mappings = {}
        for email, label, archive, mark_read in cursor.fetchall():
            fallback_mappings[email] = _build_mapping_dict(label, archive, mark_read)

        # Construct config dict
        config_dict: dict[str, Any] = {
            "version": version,
            "label_prefix": label_prefix,
            "intents": intents,
            "email_categories": email_categories,
            "prompts": prompts,
            "body_extraction_prompts": body_extraction_prompts,
            "chains": chains,
        }

        if priority_mappings:
            config_dict["priority_email_mappings"] = priority_mappings

        if fallback_mappings:
            config_dict["fallback_email_mappings"] = fallback_mappings

        # Convert to YAML
        return yaml.dump(
            config_dict,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    finally:
        cursor.close()


def import_yaml_to_db(
    conn: psycopg2.extensions.connection,
    yaml_content: str,
    created_by: str,
    notes: str | None = None,
) -> int:
    """Import YAML config to database, creating new version.

    Args:
        conn: Database connection
        yaml_content: YAML config content
        created_by: User/system creating this version
        notes: Optional notes about this version

    Returns:
        New version number

    Raises:
        ConfigImportError: If import fails
    """
    cursor = conn.cursor()

    try:
        # 1. Parse and validate YAML
        config = load_rules_from_string(yaml_content)
        errors = validate_rules(config)
        if errors:
            raise ConfigImportError(f"Invalid config: {errors}")

        # 2. Calculate hash for dedup
        config_hash = hashlib.sha256(yaml_content.encode()).hexdigest()

        # 3. Check for duplicate
        cursor.execute(
            "SELECT version FROM triage_config_versions WHERE config_hash = %s",
            (config_hash,),
        )
        existing = cursor.fetchone()
        if existing:
            version_num: int = existing[0]
            logger.info(
                f"Config already exists as version {version_num} "
                f"(hash: {config_hash[:8]}...)"
            )
            return version_num

        # 4. Create new version (transaction)
        # Serialize Pydantic models to dicts
        intents_dict = {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in config.intents.items()
        }
        email_categories_dict = {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in config.email_categories.items()
        }
        prompts_dict = {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in config.prompts.items()
        }
        body_extraction_prompts_dict = {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in config.body_extraction_prompts.items()
        }

        cursor.execute(
            """
            INSERT INTO triage_config_versions (
                created_by,
                notes,
                is_active,
                config_hash,
                label_prefix,
                intents,
                email_categories,
                prompts,
                body_extraction_prompts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING version
            """,
            (
                created_by,
                notes,
                True,  # Auto-deactivates old version via trigger
                config_hash,
                config.label_prefix,
                json.dumps(intents_dict),
                json.dumps(email_categories_dict),
                json.dumps(prompts_dict),
                json.dumps(body_extraction_prompts_dict),
            ),
        )
        result = cursor.fetchone()
        assert result is not None
        new_version: int = result[0]

        # 5. Insert chains and rules with linked list
        for chain_name, rules in config.chains.items():
            cursor.execute(
                """
                INSERT INTO triage_chains (config_version, chain_name)
                VALUES (%s, %s)
                RETURNING id
                """,
                (new_version, chain_name),
            )
            chain_result = cursor.fetchone()
            assert chain_result is not None
            chain_id: int = chain_result[0]

            prev_rule_id = None
            for rule in rules:
                # Extract rule fields
                match_condition = rule.match.model_dump() if rule.match else {}
                action = rule.action.model_dump() if rule.action else None
                jump_to_chain = rule.jump
                return_to_parent = rule.return_to_parent
                # Serialize variables dict (each value is a Variable Pydantic model)
                variables = (
                    {k: v.model_dump() for k, v in rule.variables.items()}
                    if rule.variables
                    else None
                )
                llm_config = rule.llm.model_dump() if rule.llm else None
                routes = rule.routes if rule.routes else None

                cursor.execute(
                    """
                    INSERT INTO triage_rules (
                        chain_id,
                        config_version,
                        prev_rule_id,
                        match_condition,
                        action,
                        jump_to_chain,
                        return_to_parent,
                        variables,
                        llm_config,
                        routes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        chain_id,
                        new_version,
                        prev_rule_id,
                        json.dumps(match_condition),
                        json.dumps(action) if action else None,
                        jump_to_chain,
                        return_to_parent,
                        json.dumps(variables) if variables else None,
                        json.dumps(llm_config) if llm_config else None,
                        json.dumps(routes) if routes else None,
                    ),
                )
                rule_result = cursor.fetchone()
                assert rule_result is not None
                rule_id: int = rule_result[0]

                # Update previous rule's next pointer
                if prev_rule_id:
                    cursor.execute(
                        "UPDATE triage_rules SET next_rule_id = %s WHERE id = %s",
                        (rule_id, prev_rule_id),
                    )

                prev_rule_id = rule_id

        # 6. Insert email mappings
        for email, action_config in config.priority_email_mappings.items():
            cursor.execute(
                """
                INSERT INTO triage_email_mappings (
                    config_version,
                    mapping_type,
                    email_address,
                    label,
                    archive,
                    mark_read
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    new_version,
                    "priority",
                    email.lower(),
                    action_config.label,
                    action_config.archive,
                    action_config.mark_read,
                ),
            )

        for email, action_config in config.fallback_email_mappings.items():
            cursor.execute(
                """
                INSERT INTO triage_email_mappings (
                    config_version,
                    mapping_type,
                    email_address,
                    label,
                    archive,
                    mark_read
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    new_version,
                    "fallback",
                    email.lower(),
                    action_config.label,
                    action_config.archive,
                    action_config.mark_read,
                ),
            )

        conn.commit()
        logger.info(f"Imported config as version {new_version}")
        return new_version

    except Exception as e:
        conn.rollback()
        raise ConfigImportError(f"Failed to import config: {e}") from e
    finally:
        cursor.close()


def _traverse_chain(
    cursor: psycopg2.extensions.cursor, chain_id: int
) -> list[dict[str, Any]]:
    """Traverse linked list to get rules in order.

    Uses recursive CTE to follow next_rule_id pointers.
    """
    cursor.execute(
        """
        WITH RECURSIVE chain_walk AS (
            -- Base: find head (prev_rule_id IS NULL)
            SELECT
                r.*,
                0 AS position
            FROM triage_rules r
            WHERE r.chain_id = %s AND r.prev_rule_id IS NULL

            UNION ALL

            -- Recursive: follow next_rule_id
            SELECT
                r.*,
                cw.position + 1
            FROM triage_rules r
            JOIN chain_walk cw ON r.id = cw.next_rule_id
        )
        SELECT
            id,
            match_condition,
            variables,
            action,
            jump_to_chain,
            return_to_parent,
            llm_config,
            routes,
            position
        FROM chain_walk
        ORDER BY position
        """,
        (chain_id,),
    )

    rules = []
    for row in cursor.fetchall():
        (
            rule_id,
            match_condition,
            variables,
            action,
            jump_to_chain,
            return_to_parent,
            llm_config,
            routes,
            position,
        ) = row

        rules.append(
            {
                "id": rule_id,
                "match_condition": match_condition,
                "variables": variables,
                "action": action,
                "jump_to_chain": jump_to_chain,
                "return_to_parent": return_to_parent,
                "llm_config": llm_config,
                "routes": routes,
                "position": position,
            }
        )

    return rules


def _rule_to_dict(rule: dict[str, Any]) -> dict[str, Any]:
    """Convert rule database row to YAML-compatible dict."""
    result: dict[str, Any] = {}

    # Match condition (required)
    if rule["match_condition"]:
        result["match"] = rule["match_condition"]

    # Variables (optional)
    if rule["variables"]:
        result["variables"] = rule["variables"]

    # Outcome (exactly one of: action, jump, return_to_parent, llm)
    if rule["action"]:
        result["action"] = rule["action"]
    elif rule["jump_to_chain"]:
        result["jump"] = rule["jump_to_chain"]
    elif rule["return_to_parent"]:
        result["return_to_parent"] = True
    elif rule["llm_config"]:
        result["llm"] = rule["llm_config"]

    # Routes (optional, only for LLM)
    if rule["routes"]:
        result["routes"] = rule["routes"]

    return result


def _build_mapping_dict(
    label: str, archive: bool | None, mark_read: bool | None
) -> dict[str, Any]:
    """Build email mapping dict for YAML export."""
    mapping: dict[str, Any] = {"label": label}

    if archive is not None:
        mapping["archive"] = archive

    if mark_read is not None:
        mapping["mark_read"] = mark_read

    return mapping
