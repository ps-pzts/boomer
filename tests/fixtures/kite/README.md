# Kite Connect response fixtures

These files are what Kite Connect v3 sends over the wire, written from the
public documentation (https://kite.trade/docs/connect/v3/): orders, portfolio,
user margins, market quotes, GTT, historical candles, session, and WebSocket
order updates (postbacks).

They are **documentation-derived, not recorded**. The live canary (#85) will
record real responses and replace them one by one; `_meta.json` says where each
file currently comes from.

Rules:

- A file is the raw HTTP body exactly as Kite returns it (the
  `{"status": "success", "data": ...}` envelope), the raw CSV for
  `instruments`, or the raw WebSocket message for `order_postback`.
- No provenance or comments inside payloads. Provenance lives in `_meta.json`.
- Tests never hand-type a Kite response. They get one from
  `tests/kite_fixtures.py`: `load(name)` (the file), `data(name)` (the payload),
  or `sdk(method, ...)`, which runs the real `kiteconnect.KiteConnect` method
  against the file through a fake HTTP session, so the value has exactly the
  Python shape the SDK hands KiteBroker (parsed timestamps, parsed CSV, the
  bare order-id string from `place_order`).
- Every shape is checked by `executor.brokers.kite_contract.check_shape`
  (`tests/executor/brokers/test_kite_contract.py`).

`orders.json` covers the order lifecycle: COMPLETE, TRIGGER PENDING (SL-M),
OPEN (LIMIT), OPEN part-filled, MODIFY PENDING, CANCEL PENDING, CANCELLED, and
REJECTED twice (insufficient margin; price outside the circuit band).
