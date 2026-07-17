import pytest


@pytest.fixture
def receipt():
    return {
        "receipt_id": 12345,
        "name": "Alex Buyer",
        "first_line": "42 Test Lane",
        "second_line": "",
        "city": "Denver",
        "state": "CO",
        "zip": "80202",
        "country_iso": "US",
        "created_timestamp": 1_752_000_000,
        "is_gift": False,
        "message_from_buyer": "",
        "grandtotal": {"amount": 2599, "divisor": 100, "currency_code": "USD"},
        "transactions": [
            {
                "title": "Walnut phone stand",
                "quantity": 1,
                "sku": "STAND-WAL",
                "variations": [
                    {"formatted_name": "Finish", "formatted_value": "Matte"}
                ],
            },
            {"title": "Sticker pack", "quantity": 3, "sku": "", "variations": []},
        ],
    }
