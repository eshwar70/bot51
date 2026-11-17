#!/usr/bin/env python3
"""Self-test and validation suite for Winner of 51."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import unittest

from core import IST, stamp
from fast_practice import QuickPracticeEngine, load_fast_config

DIR_PATH = Path(__file__).resolve().parent


class TestWinner51(unittest.TestCase):
    def test_configs_load(self):
        c50 = load_fast_config(DIR_PATH / "config_fast_practice_50k.json")
        self.assertEqual(c50["initial_wallet"], 50000.0)
        self.assertEqual(c50["capital_floor"], 30000.0)
        self.assertEqual(c50["max_trades_day"], 4)

        c100 = load_fast_config(DIR_PATH / "config_fast_practice_100k.json")
        self.assertEqual(c100["initial_wallet"], 100000.0)
        self.assertEqual(c100["capital_floor"], 60000.0)

    def test_engine_init_and_reports(self):
        c = load_fast_config(DIR_PATH / "config_fast_practice_50k.json")
        test_dir = DIR_PATH / "scratch_unit_test"
        if test_dir.exists():
            shutil.rmtree(test_dir)
        try:
            eng = QuickPracticeEngine(c, test_dir)
            self.assertEqual(eng.s["equity"], 50000.0)
            self.assertEqual(eng.s["cash"], 50000.0)
            rep = eng.reports()
            self.assertEqual(rep["equity"], 50000.0)
            self.assertTrue((test_dir / "summary.json").exists())
        finally:
            if test_dir.exists():
                shutil.rmtree(test_dir)

    def test_step_trailing_ladder(self):
        c = load_fast_config(DIR_PATH / "config_fast_practice_50k.json")
        ladder = c["step_trail_levels"]
        self.assertEqual(len(ladder), 4)
        self.assertEqual(ladder[0], [0.085, 0.035])
        self.assertEqual(ladder[3], [0.28, 0.215])


if __name__ == "__main__":
    unittest.main()
