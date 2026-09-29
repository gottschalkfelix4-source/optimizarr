"""Idempotent additive upgrades for installations with an existing SQLite DB."""
from sqlalchemy import inspect


def upgrade(engine) -> None:
    # Unknown historic measurements stay unknown, never relabelled as real VMAF.
    with engine.begin() as connection:
        for table in ("jobs", "media_files", "learning_samples"):
            columns = {c["name"] for c in inspect(connection).get_columns(table)}
            for name, ddl in (("quality_metric", "VARCHAR(16)"), ("quality_value", "FLOAT")):
                if name not in columns:
                    connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
