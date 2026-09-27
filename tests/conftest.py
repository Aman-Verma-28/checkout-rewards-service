"""Tests talk HTTP to a real uvicorn server, so concurrent requests really overlap in the
server's thread pool, the way an evaluator's load would, instead of running one at a time."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

from app.main import create_app

EVERY_N, PERCENT = 2, 10


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "store.db"), every_n=EVERY_N, percent=PERCENT)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server failed to start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        yield client
    server.should_exit = True
    thread.join()


def post_concurrently(api, requests):
    """POST every (path, json) from its own thread and connection, all released at once."""
    barrier = threading.Barrier(len(requests))

    def send(request):
        path, body = request
        with httpx.Client(base_url=api.base_url, timeout=30) as client:
            barrier.wait()
            return client.post(path, json=body)

    with ThreadPoolExecutor(len(requests)) as pool:
        return list(pool.map(send, requests))


def cart_with(api, items: dict) -> str:
    cart_id = api.post("/carts").json()["id"]
    for product_id, quantity in items.items():
        r = api.put(f"/carts/{cart_id}/items/{product_id}", json={"quantity": quantity})
        assert r.status_code == 200, r.text
    return cart_id


def place_order(api, items: dict, **body) -> dict:
    r = api.post(f"/carts/{cart_with(api, items)}/checkout", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def earn_coupon(api) -> str:
    """Place enough orders to reach the next milestone, then have the admin issue its coupon."""
    report = api.get("/admin/report").json()
    for _ in range(report["rewards"]["next_milestone"] - report["orders_placed"]):
        place_order(api, {"cap": 1})
    r = api.post("/admin/coupons")
    assert r.status_code == 201, r.text
    return r.json()["code"]


def stock(api, product_id: str) -> int:
    return next(p["inventory"] for p in api.get("/products").json() if p["id"] == product_id)


def error_code(response) -> str:
    return response.json()["error"]["code"]
