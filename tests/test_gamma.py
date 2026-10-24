import unittest

from _load import load

gm = load("trader-timemachine", "gamma")


class Levels(unittest.TestCase):
    def test_walls_zero_and_total(self):
        conts = [{"symbol": "P90", "type": "put", "strike_price": "90", "open_interest": "1000"},
                 {"symbol": "P95", "type": "put", "strike_price": "95", "open_interest": "500"},
                 {"symbol": "C105", "type": "call", "strike_price": "105", "open_interest": "800"},
                 {"symbol": "C110", "type": "call", "strike_price": "110", "open_interest": "3000"},
                 {"symbol": "C120", "type": "call", "strike_price": "120", "open_interest": None}]
        snaps = {s: {"greeks": {"gamma": 0.02}} for s in ("P90", "P95", "C105", "C110", "C120")}
        lv = gm.levels(conts, snaps, 100.0)
        self.assertEqual((lv["call_wall"], lv["put_wall"]), (110, 90))
        self.assertEqual(lv["zero_gamma"], 110)       # the running total turns positive at 110
        self.assertEqual(lv["strikes"], 4)            # no open interest: left out
        self.assertGreater(lv["total"], 0)

    def test_nothing_usable(self):
        self.assertIsNone(gm.levels([{"symbol": "X", "type": "call", "strike_price": "1", "open_interest": "5"}],
                                    {}, 10.0))


if __name__ == "__main__":
    unittest.main()
