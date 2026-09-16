CREATE TABLE regions (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  region_id INTEGER REFERENCES regions(id),
  name TEXT NOT NULL
);

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  amount REAL,
  status TEXT NOT NULL
);

INSERT INTO regions VALUES (1, 'East'), (2, 'North');
INSERT INTO customers VALUES
  (10, 1, 'Alice'),
  (11, 2, 'Bob'),
  (12, NULL, 'Carol');
INSERT INTO orders VALUES
  (100, 10, 120.0, 'paid'),
  (101, 10, 80.0, 'paid'),
  (102, 11, 200.0, 'paid'),
  (103, 12, NULL, 'pending'),
  (104, NULL, 30.0, 'cancelled');
