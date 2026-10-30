from copy import deepcopy


def sample_shop():
    address = dict(
        address1="Учебная 1",
        address2="",
        city="Город",
        state="CA",
        country="US",
        zip="12345",
    )
    products = {
        "p1": {
            "name": "Наушники",
            "product_id": "p1",
            "variants": {
                "old": {
                    "item_id": "old",
                    "options": {"color": "black"},
                    "available": True,
                    "price": 10.0,
                },
                "new": {
                    "item_id": "new",
                    "options": {"color": "white"},
                    "available": True,
                    "price": 12.35,
                },
                "cheap": {
                    "item_id": "cheap",
                    "options": {"color": "blue"},
                    "available": True,
                    "price": 8.9,
                },
            },
        },
        "p2": {
            "name": "Чайник",
            "product_id": "p2",
            "variants": {
                "foreign": {
                    "item_id": "foreign",
                    "options": {},
                    "available": True,
                    "price": 20.0,
                },
            },
        },
    }
    users = {}
    orders = {}
    for number in (1, 2):
        uid, oid = f"u{number}", f"#O{number}"
        item_id, product_id = ("old", "p1") if number == 1 else ("foreign", "p2")
        users[uid] = {
            "user_id": uid,
            "name": {"first_name": f"Имя{number}", "last_name": "Фамилия"},
            "address": deepcopy(address),
            "email": f"user{number}@example.test",
            "orders": [oid],
            "payment_methods": {
                "card": {
                    "source": "credit_card",
                    "id": "card",
                    "brand": "visa",
                    "last_four": "1111",
                },
                "other_card": {
                    "source": "credit_card",
                    "id": "other_card",
                    "brand": "visa",
                    "last_four": "2222",
                },
                "gift": {"source": "gift_card", "id": "gift", "balance": 5.0},
            },
        }
        orders[oid] = {
            "order_id": oid,
            "user_id": uid,
            "address": deepcopy(address),
            "status": "delivered",
            "items": [
                {
                    "name": products[product_id]["name"],
                    "product_id": product_id,
                    "item_id": item_id,
                    "price": 10.0,
                    "options": {"color": "black"},
                }
            ],
            "fulfillments": [],
            "payment_history": [
                {
                    "transaction_type": "payment",
                    "amount": 10.0,
                    "payment_method_id": "card",
                }
            ],
        }
    return {"products": products, "users": users, "orders": orders}
