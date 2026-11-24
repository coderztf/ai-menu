-- SQLite 3.38+ with JSON support. Apply once to a new database.




CREATE TABLE household_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  stock_version INTEGER NOT NULL DEFAULT 0 CHECK (stock_version >= 0)
) STRICT;
INSERT INTO household_state (id) VALUES (1);

CREATE TABLE ingredient (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE CHECK (length(code) <= 64),
  name TEXT NOT NULL CHECK (length(name) <= 80),
  stock_policy TEXT NOT NULL CHECK (stock_policy IN ('tracked','unlimited','pantry')),
  food_group TEXT NOT NULL CHECK (food_group IN ('meat','egg','plant','other_animal'))
) STRICT;

CREATE TABLE ingredient_alias (
  alias TEXT NOT NULL PRIMARY KEY CHECK (length(alias) <= 80),
  ingredient_id INTEGER NOT NULL,
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id)
) STRICT;

CREATE TABLE inventory (
  ingredient_id INTEGER PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('enough','low','empty')),
  note TEXT NULL CHECK (length(note) <= 200),
  prioritize INTEGER NOT NULL DEFAULT FALSE,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id),
  CHECK (prioritize IN (0,1)),
  CHECK (state <> 'empty' OR (note IS NULL AND prioritize = 0))
) STRICT;

CREATE TABLE recipe (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE CHECK (length(code) <= 64),
  name TEXT NOT NULL CHECK (length(name) <= 100),
  kind TEXT NOT NULL CHECK (kind IN ('dish','soup','staple')),
  minutes INTEGER NOT NULL CHECK (minutes > 0),
  instructions TEXT NOT NULL CHECK (json_type(instructions) = 'array'),
  active INTEGER NOT NULL DEFAULT TRUE CHECK (active IN (0,1))
) STRICT;

CREATE TABLE recipe_ingredient (
  recipe_id INTEGER NOT NULL,
  ingredient_id INTEGER NOT NULL,
  amount_hint TEXT NOT NULL CHECK (length(amount_hint) <= 80),
  accepts_low INTEGER NOT NULL DEFAULT FALSE CHECK (accepts_low IN (0,1)),
  PRIMARY KEY (recipe_id, ingredient_id),
  FOREIGN KEY (recipe_id) REFERENCES recipe(id),
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id)
) STRICT;

CREATE TABLE daily_menu (
  menu_date TEXT NOT NULL PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  stock_version INTEGER NOT NULL CHECK (stock_version >= 0),
  status TEXT NOT NULL CHECK (status IN ('ready','insufficient','cooked')),
  diagnostics TEXT NULL CHECK (diagnostics IS NULL OR json_valid(diagnostics)),
  generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE daily_menu_item (
  menu_date TEXT NOT NULL,
  slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
  recipe_id INTEGER NOT NULL,
  snapshot TEXT NOT NULL CHECK (json_type(snapshot) = 'object'),
  PRIMARY KEY (menu_date, slot),
  UNIQUE (menu_date, recipe_id),
  FOREIGN KEY (menu_date) REFERENCES daily_menu(menu_date),
  FOREIGN KEY (recipe_id) REFERENCES recipe(id)
) STRICT;

CREATE TABLE write_request (
  request_key TEXT NOT NULL PRIMARY KEY CHECK (length(request_key) = 36),
  operation TEXT NOT NULL CHECK (length(operation) <= 80),
  payload_hash BLOB NOT NULL CHECK (length(payload_hash) = 32),
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL CHECK (response_json IS NULL OR json_valid(response_json)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE cooking_event (
  menu_date TEXT NOT NULL PRIMARY KEY,
  menu_revision INTEGER NOT NULL CHECK (menu_revision > 0),
  cooked_slots TEXT NOT NULL CHECK (json_type(cooked_slots) = 'array'),
  request_key TEXT NOT NULL UNIQUE CHECK (length(request_key) = 36),
  confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  FOREIGN KEY (menu_date) REFERENCES daily_menu(menu_date),
  FOREIGN KEY (request_key) REFERENCES write_request(request_key)
) STRICT;

CREATE TABLE inventory_event (
  request_key TEXT NOT NULL CHECK (length(request_key) = 36),
  ingredient_id INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('intake','cooking')),
  before_value TEXT NULL CHECK (before_value IS NULL OR json_valid(before_value)),
  after_value TEXT NOT NULL CHECK (after_value IS NULL OR json_valid(after_value)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (request_key, ingredient_id),
  FOREIGN KEY (request_key) REFERENCES write_request(request_key),
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id)
) STRICT;

INSERT INTO ingredient (code,name,stock_policy,food_group) VALUES
 ('pork','猪肉','unlimited','meat'), ('egg','鸡蛋','unlimited','egg'),
 ('beef','牛肉','tracked','meat'), ('lamb','羊肉','tracked','meat'),
 ('chicken','鸡肉','tracked','meat'), ('tomato','番茄','tracked','plant'),
 ('potato','土豆','tracked','plant'), ('lettuce','生菜','tracked','plant'),
 ('garlic','大蒜','pantry','plant'), ('salt','盐','pantry','plant');
INSERT INTO ingredient_alias (alias,ingredient_id)
 SELECT name,id FROM ingredient;
INSERT INTO ingredient_alias (alias,ingredient_id)
 SELECT '西红柿',id FROM ingredient WHERE code='tomato';

CREATE INDEX ix_recipe_history ON daily_menu_item (recipe_id, menu_date);
CREATE INDEX ix_cooking_time ON cooking_event (confirmed_at, menu_date);
CREATE INDEX ix_ingredient_history ON inventory_event (ingredient_id, created_at);

PRAGMA user_version = 1;

