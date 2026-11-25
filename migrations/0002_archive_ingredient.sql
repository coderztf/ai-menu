CREATE TABLE archived_ingredient (
  ingredient_id INTEGER PRIMARY KEY REFERENCES ingredient(id),
  archived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
