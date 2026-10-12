from backend.analyzers.sql_analyzer import analyze_statement, looks_like_sql, looks_like_sql_strict, split_statements


def test_select_join_and_cte_excluded():
    f = analyze_statement("WITH a AS (SELECT * FROM users) SELECT * FROM a JOIN orders o ON o.uid = a.id")
    assert set(f.reads) == {"users", "orders"} and not f.writes


def test_insert_select():
    f = analyze_statement("INSERT INTO audit (a, b) SELECT a, b FROM events")
    assert f.writes == ["audit"] and f.reads == ["events"]


def test_update_with_subquery_and_delete():
    f = analyze_statement("UPDATE owners SET x = 1 WHERE id IN (SELECT owner_id FROM pets)")
    assert f.writes == ["owners"] and f.reads == ["pets"]
    assert analyze_statement("DELETE FROM sessions WHERE id = ?").writes == ["sessions"]


def test_extract_from_is_not_a_table():
    f = analyze_statement("SELECT extract(year FROM created_at) FROM invoices")
    assert f.reads == ["invoices"]


def test_string_literals_ignored():
    f = analyze_statement("SELECT * FROM t WHERE note = 'select * from secret'")
    assert f.reads == ["t"]


def test_create_table_columns_pk_fk():
    f = analyze_statement("CREATE TABLE pets (id INT PRIMARY KEY, owner_id INT NOT NULL, INDEX(owner_id), "
                          "FOREIGN KEY (owner_id) REFERENCES owners(id))")
    td = f.defines[0]
    assert [c["name"] for c in td.columns] == ["id", "owner_id"]
    assert td.columns[0]["pk"] and not td.columns[1]["nullable"]
    assert td.fks == [("owner_id", "owners", "id")]


def test_alter_fk():
    f = analyze_statement("ALTER TABLE pets ADD CONSTRAINT fk FOREIGN KEY (type_id) REFERENCES types (id)")
    assert f.alter_fks == [("pets", "type_id", "types", "id")]


def test_jpql_entity_mapping():
    f = analyze_statement("SELECT o FROM Owner o WHERE o.lastName = :n", {"Owner": "owners"})
    assert f.reads == ["owners"]


def test_sql_detection():
    assert looks_like_sql("SELECT id FROM users")
    assert not looks_like_sql_strict("Select the item from the list")
    assert not looks_like_sql("hello world")


def test_split_statements_lines():
    stmts = split_statements("-- c\nCREATE TABLE a (id int);\n\nINSERT INTO a VALUES (1);\n")
    assert [l for _, l in stmts] == [2, 4]
