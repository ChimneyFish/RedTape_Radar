from sqlalchemy import inspect, text


def run_migrations(engine, base):
    """Bring the database schema up to date with the current models, in place.

    SQLite can't do most ALTER TABLE operations, so this only ever adds tables
    and columns that are missing -- it never renames or drops one. That covers
    every schema change this app has made so far, and means an existing
    database (targets, users, scan history, config) survives a code update
    instead of needing to be wiped and rebuilt.
    """
    base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in base.metadata.sorted_tables:
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                try:
                    conn.execute(text(ddl))
                    print(f"Migration: added column {table.name}.{column.name}")
                except Exception as e:
                    print(f"Migration failed for {table.name}.{column.name}: {e}")
