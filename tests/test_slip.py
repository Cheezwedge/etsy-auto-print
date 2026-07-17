from etsy_auto_print.slip import render_packing_slip


def test_slip_contains_address_and_items(receipt):
    slip = render_packing_slip(receipt)
    assert "Order #12345" in slip
    assert "Alex Buyer" in slip
    assert "42 Test Lane" in slip
    assert "Denver CO 80202" in slip
    assert "1 x Walnut phone stand" in slip
    assert "SKU: STAND-WAL" in slip
    assert "Finish: Matte" in slip
    assert "3 x Sticker pack" in slip
    assert "25.99 USD" in slip


def test_slip_prefers_formatted_address(receipt):
    receipt["formatted_address"] = "Alex Buyer\n42 Test Lane\nDenver, CO 80202\nUnited States"
    slip = render_packing_slip(receipt)
    assert "Denver, CO 80202" in slip


def test_gift_and_buyer_note(receipt):
    receipt.update(is_gift=True, gift_message="Congrats!", message_from_buyer="Ring the bell")
    slip = render_packing_slip(receipt)
    assert "** GIFT **" in slip
    assert "Congrats!" in slip
    assert "Ring the bell" in slip


def test_minimal_receipt_does_not_crash():
    slip = render_packing_slip({"receipt_id": 1})
    assert "Order #1" in slip
