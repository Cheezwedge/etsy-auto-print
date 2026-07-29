from etsy_auto_print.cli import _redact


def test_redacts_pii_but_keeps_structure():
    out = _redact({"receipt_id": 1, "name": "Jane Doe", "state": "OR"})
    assert out["receipt_id"] == 1
    assert out["state"] == "OR"  # not PII: needed for shipping logic
    assert "Jane" not in out["name"]


def test_null_and_empty_are_preserved_verbatim():
    # The whole point of inspection is seeing which fields Etsy populates,
    # so absent values must stay distinguishable from redacted ones.
    out = _redact({"shipping_method": None, "second_line": "", "city": "Portland"})
    assert out["shipping_method"] is None
    assert out["second_line"] == ""
    assert out["city"].startswith("<redacted")


def test_redacts_inside_nested_lists_and_dicts():
    out = _redact({"transactions": [{"sku": "MUG-1", "buyer_email": "a@b.com"}]})
    txn = out["transactions"][0]
    assert txn["sku"] == "MUG-1"
    assert "a@b.com" not in txn["buyer_email"]
